#!/usr/bin/python3
"""Collect Grok CLI usage into one Omarchy agents-panel JSON record.

Omarchy's stock updater only ships Claude, Codex, and Fireworks collectors.
This writes the same record contract for Grok from:

- ~/.grok/sessions/**/updates.jsonl  (per-turn token usage)
- https://cli-chat-proxy.grok.com/v1/billing?format=credits  (weekly pool)

The agents panel watches ~/.local/state/omarchy/agents/usage/*.json and
draws whatever appears there, so a grok.json is enough to get a tab.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

AGENT_ID = "grok"
AGENT_NAME = "Grok"
AUTH_HELP = "Run `grok login` to restore weekly usage limits."
BILLING_ENDPOINT = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
PROBE_MIN_INTERVAL_SECONDS = 15


def expand_path(value: str) -> Path:
  return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def grok_home() -> Path:
  return expand_path(os.environ.get("GROK_HOME") or "~/.grok")


def cache_root() -> Path:
  root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "omarchy" / "agent-usage"
  root.mkdir(parents=True, exist_ok=True)
  return root


def usage_dir() -> Path:
  state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
  path = state / "omarchy" / "agents" / "usage"
  path.mkdir(parents=True, exist_ok=True)
  return path


def date_string(value: dt.date) -> str:
  return value.strftime("%Y-%m-%d")


def recent_date_strings() -> list[str]:
  today = dt.datetime.now().date()
  return [date_string(today - dt.timedelta(days=offset)) for offset in range(6, -1, -1)]


def local_date_string() -> str:
  return date_string(dt.datetime.now().date())


def local_date_from_timestamp(value: Any) -> str:
  if value is None:
    return local_date_string()
  try:
    seconds = float(value)
  except (TypeError, ValueError):
    return local_date_string()
  if seconds > 10_000_000_000:
    seconds /= 1000.0
  try:
    return date_string(dt.datetime.fromtimestamp(seconds).date())
  except Exception:
    return local_date_string()


def number(value: Any) -> int:
  try:
    n = float(value or 0)
    return round(n) if n == n else 0
  except Exception:
    return 0


def empty_bucket() -> dict[str, int]:
  return {
    "inputTokens": 0,
    "outputTokens": 0,
    "cacheReadInputTokens": 0,
    "cacheCreationInputTokens": 0,
  }


def empty_stats() -> dict[str, Any]:
  recent = [{"date": day, "messageCount": 0} for day in recent_date_strings()]
  return {
    "todayPrompts": 0,
    "todaySessions": 0,
    "todayTotalTokens": 0,
    "todayTokensByModel": {},
    "recentDays": recent,
    "modelUsage": {},
    "totalPrompts": 0,
    "totalSessions": 0,
    "activeDays": 0,
    "activeDates": [],
  }


def write_json(path: Path, payload: dict[str, Any]) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  handle_fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
  tmp = Path(tmp_name)
  try:
    with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
      handle.write(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")
    tmp.chmod(0o600)
    tmp.replace(path)
  except BaseException:
    tmp.unlink(missing_ok=True)
    raise


def read_fresh_json(path: Path, max_age_seconds: float) -> dict[str, Any] | None:
  if max_age_seconds <= 0 or not path.exists():
    return None
  try:
    if time.time() - path.stat().st_mtime <= max_age_seconds:
      data = json.loads(path.read_text(encoding="utf-8"))
      return data if isinstance(data, dict) else None
  except Exception:
    return None
  return None


# ---------------------------------------------------------------- local scan


def scan_sessions(sessions_dir: Path) -> dict[str, Any]:
  today = local_date_string()
  recent_dates = recent_date_strings()
  recent = {day: {"date": day, "messageCount": 0} for day in recent_dates}

  seen: set[str] = set()
  sessions: set[str] = set()
  active_days: set[str] = set()
  today_sessions: set[str] = set()
  today_tokens: dict[str, int] = {}
  usage_by_model: dict[str, dict[str, int]] = {}
  prompts = 0
  today_prompt_count = 0
  today_token_total = 0

  files = sessions_dir.glob("*/*/updates.jsonl") if sessions_dir.is_dir() else []
  for path in files:
    session_id = path.parent.name
    try:
      with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
          if "turn_completed" not in line or '"usage"' not in line:
            continue
          try:
            entry = json.loads(line)
          except Exception:
            continue

          params = entry.get("params") if isinstance(entry, dict) else None
          update = params.get("update") if isinstance(params, dict) else None
          if not isinstance(update, dict) or update.get("sessionUpdate") != "turn_completed":
            continue
          usage = update.get("usage")
          if not isinstance(usage, dict):
            continue

          prompt_id = str(update.get("prompt_id") or "")
          sid = str(params.get("sessionId") or session_id)
          key = sid + ":" + (prompt_id or str(entry.get("timestamp") or ""))
          if key in seen:
            continue
          seen.add(key)

          input_tokens = number(usage.get("inputTokens"))
          output_tokens = number(usage.get("outputTokens"))
          cache_read = number(usage.get("cachedReadTokens") or usage.get("cacheReadInputTokens"))
          cache_write = number(usage.get("cacheCreationTokens") or usage.get("cachedWriteTokens"))
          total = input_tokens + output_tokens + cache_read + cache_write
          if total <= 0:
            continue

          meta = entry.get("_meta") if isinstance(entry.get("_meta"), dict) else {}
          day = local_date_from_timestamp(meta.get("agentTimestampMs") or entry.get("timestamp"))
          models = usage.get("modelUsage") if isinstance(usage.get("modelUsage"), dict) else {}
          model = next(iter(models), None) or "grok"
          model = str(model).rstrip("/").split("/")[-1] or "grok"

          sessions.add(sid)
          active_days.add(day)
          prompts += 1
          bucket = usage_by_model.setdefault(model, empty_bucket())
          bucket["inputTokens"] += input_tokens
          bucket["outputTokens"] += output_tokens
          bucket["cacheReadInputTokens"] += cache_read
          bucket["cacheCreationInputTokens"] += cache_write
          if day in recent:
            recent[day]["messageCount"] += total
          if day == today:
            today_prompt_count += 1
            today_sessions.add(sid)
            today_token_total += total
            today_tokens[model] = today_tokens.get(model, 0) + total
    except OSError:
      continue

  if prompts <= 0:
    return empty_stats()
  return {
    "todayPrompts": today_prompt_count,
    "todaySessions": len(today_sessions),
    "todayTotalTokens": today_token_total,
    "todayTokensByModel": today_tokens,
    "recentDays": [recent[day] for day in recent_dates],
    "modelUsage": usage_by_model,
    "totalPrompts": prompts,
    "totalSessions": len(sessions),
    "activeDays": len(active_days),
    "activeDates": sorted(active_days),
  }


def cached_scan(sessions_dir: Path, max_age_seconds: float) -> dict[str, Any]:
  cache_file = cache_root() / "grok-sessions.json"
  lock_file = cache_root() / "grok-sessions.lock"
  cached = read_fresh_json(cache_file, max_age_seconds)
  if cached is not None and "totalPrompts" in cached:
    return cached
  with lock_file.open("w") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    cached = read_fresh_json(cache_file, max_age_seconds)
    if cached is not None and "totalPrompts" in cached:
      return cached
    stats = scan_sessions(sessions_dir)
    write_json(cache_file, stats)
    return stats


# ------------------------------------------------------------------- limits


def access_token(auth_path: Path) -> tuple[str, str]:
  try:
    data = json.loads(auth_path.read_text(encoding="utf-8"))
  except Exception:
    return "", ""
  if not isinstance(data, dict):
    return "", ""
  for entry in data.values():
    if not isinstance(entry, dict):
      continue
    token = str(entry.get("key") or "").strip()
    if not token:
      continue
    expires = str(entry.get("expires_at") or "")
    return token, expires
  return "", ""


def token_expired(expires_at: str) -> bool:
  if not expires_at:
    return False
  try:
    parsed = dt.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
  except Exception:
    return False
  if parsed.tzinfo is None:
    parsed = parsed.replace(tzinfo=dt.timezone.utc)
  return parsed <= dt.datetime.now(dt.timezone.utc)


def normalize_percent(value: Any) -> float:
  try:
    n = float(value)
  except (TypeError, ValueError):
    return -1.0
  if n != n or n < 0:
    return -1.0
  # Grok billing reports 40.0 for forty percent.
  return min(1.0, n / 100.0)


def product_label(raw: Any) -> str:
  text = str(raw or "").strip()
  if not text:
    return "Grok"
  spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
  spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", spaced)
  return spaced.strip() or text


def normalize_reset_at(value: Any) -> str:
  raw = str(value or "").strip()
  if not raw:
    return ""
  try:
    parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return parsed.isoformat()
  except Exception:
    return raw


def probe_limits(token: str) -> dict[str, Any]:
  request = urllib.request.Request(
    BILLING_ENDPOINT,
    headers={
      "Authorization": "Bearer " + token,
      "Accept": "application/json",
    },
  )
  try:
    with urllib.request.urlopen(request, timeout=10) as response:
      payload = json.loads(response.read().decode("utf-8", errors="replace"))
  except urllib.error.HTTPError as error:
    if error.code in (401, 403):
      return {"ok": False, "helpText": "Grok sign-in is no longer valid. Run `grok login`. Local Grok stats are still shown."}
    return {"ok": False, "helpText": f"Grok billing returned status {error.code}. Local Grok stats are still shown."}
  except Exception:
    return {
      "ok": False,
      "transport": True,
      "helpText": "Couldn't reach Grok billing. Retrying shortly. Local Grok stats are still shown.",
    }

  config = payload.get("config") if isinstance(payload, dict) else None
  if not isinstance(config, dict):
    return {"ok": False, "helpText": "Grok billing returned no usage. Local Grok stats are still shown."}

  period = config.get("currentPeriod") if isinstance(config.get("currentPeriod"), dict) else {}
  resets_at = normalize_reset_at(period.get("end") or config.get("billingPeriodEnd"))
  limits: list[dict[str, Any]] = []

  weekly = normalize_percent(config.get("creditUsagePercent"))
  if weekly >= 0:
    limits.append({"label": "Weekly", "title": "Weekly", "percent": weekly, "resetsAt": resets_at})

  products = config.get("productUsage")
  if isinstance(products, list):
    for entry in products:
      if not isinstance(entry, dict):
        continue
      percent = normalize_percent(entry.get("usagePercent"))
      if percent < 0:
        continue
      name = product_label(entry.get("product"))
      limits.append({"label": name, "title": name, "percent": percent, "resetsAt": resets_at})

  if not limits:
    return {"ok": False, "helpText": "Grok billing returned no limits. Local Grok stats are still shown."}

  tier = str(config.get("subscriptionTier") or payload.get("subscriptionTier") or "").strip()
  return {"ok": True, "limits": limits, "tierLabel": tier}


def limit_window_open(entry: dict[str, Any], now: dt.datetime) -> bool:
  raw = str(entry.get("resetsAt") or "")
  if raw == "":
    return True
  try:
    resets_at = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
  except Exception:
    return True
  if resets_at.tzinfo is None:
    resets_at = resets_at.replace(tzinfo=dt.timezone.utc)
  return resets_at > now


def usable_cached_limits(cached: dict[str, Any]) -> list[dict[str, Any]]:
  entries = cached.get("limits")
  if not isinstance(entries, list):
    return []
  now = dt.datetime.now(dt.timezone.utc)
  return [entry for entry in entries if isinstance(entry, dict) and limit_window_open(entry, now)]


def collect_limits(token: str, expires_at: str, force: bool) -> dict[str, Any]:
  result = {"limits": [], "usageStatusText": "", "authHelpText": AUTH_HELP, "tierLabel": ""}
  probe_cache = cache_root() / "grok-limits.json"
  cached = read_fresh_json(probe_cache, float("inf")) or {}
  fallback = usable_cached_limits(cached)
  if isinstance(cached.get("tierLabel"), str):
    result["tierLabel"] = cached["tierLabel"]

  if token == "":
    result["limits"] = fallback
    result["usageStatusText"] = "Waiting for auth"
    return result
  if token_expired(expires_at):
    result["limits"] = fallback
    result["usageStatusText"] = "Sign-in expired"
    result["authHelpText"] = (
      "Grok's saved sign-in expired"
      + (" — showing the last known limits." if fallback else ".")
      + " Start Grok, or run `grok login`, to refresh it."
    )
    return result

  fetched_at = number(cached.get("fetchedAtMs")) / 1000
  if fallback and not force and time.time() - fetched_at < PROBE_MIN_INTERVAL_SECONDS:
    result["limits"] = fallback
    return result

  probe = probe_limits(token)
  if probe["ok"]:
    result["limits"] = probe["limits"]
    result["tierLabel"] = str(probe.get("tierLabel") or result["tierLabel"])
    write_json(probe_cache, {
      "fetchedAtMs": round(time.time() * 1000),
      "limits": probe["limits"],
      "tierLabel": result["tierLabel"],
    })
    result["usageStatusText"] = ""
    result["authHelpText"] = AUTH_HELP
    return result

  if probe.get("transport"):
    result["retryAdvised"] = True
  if fallback:
    result["limits"] = fallback
  else:
    result["usageStatusText"] = "Grok limits unavailable"
    result["authHelpText"] = probe["helpText"]
  return result


# -------------------------------------------------------------------- record


def build_record(force: bool, limits_only: bool, cache_seconds: float) -> dict[str, Any]:
  home = grok_home()
  scan_age = 0 if force else (900 if limits_only else cache_seconds)
  stats = cached_scan(home / "sessions", scan_age)
  token, expires_at = access_token(home / "auth.json")
  limits = collect_limits(token, expires_at, force)

  record = {
    "schemaVersion": 1,
    "id": AGENT_ID,
    "name": AGENT_NAME,
    "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
    "ready": number(stats.get("totalPrompts")) > 0 or len(limits["limits"]) > 0,
    "hasLocalStats": number(stats.get("totalPrompts")) > 0,
    "tierLabel": limits.get("tierLabel") or "",
    "usageStatusText": limits["usageStatusText"],
    "authHelpText": limits["authHelpText"],
    "limits": limits["limits"],
  }
  if limits.get("retryAdvised"):
    record["retryAdvised"] = True
  record.update(stats)
  return record


def clear_record() -> None:
  dest = usage_dir() / "grok.json"
  dest.unlink(missing_ok=True)


def main() -> int:
  parser = argparse.ArgumentParser()
  parser.add_argument("--force", action="store_true", help="rescan sessions and re-probe billing, ignoring caches")
  parser.add_argument("--limits-only", action="store_true", help="reuse any recent session scan; only billing must be fresh")
  parser.add_argument("--cache-seconds", type=float, default=20)
  parser.add_argument("--write", action="store_true", help="write ~/.local/state/omarchy/agents/usage/grok.json")
  parser.add_argument("--clear", action="store_true", help="remove grok.json so the agents panel drops the Grok tab")
  args = parser.parse_args()

  if args.clear:
    clear_record()
    return 0

  record = build_record(args.force, args.limits_only, args.cache_seconds)
  encoded = json.dumps(record, separators=(",", ":"), sort_keys=True)
  if args.write:
    dest = usage_dir() / "grok.json"
    tmp = dest.with_name(".grok." + str(os.getpid()) + ".tmp")
    tmp.write_text(encoded + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(dest)
  else:
    print(encoded)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
