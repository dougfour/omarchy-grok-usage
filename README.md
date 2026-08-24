# Grok Usage

Adds **Grok Build** to Omarchy's built-in AI agents bar panel (`omarchy.agents`).

Omarchy already meters Claude Code, Codex, and Fireworks. This plugin writes a Grok usage record in the same place, so the existing AI icon gains a Grok chip: weekly SuperGrok pool, Grok Build vs Chat split, and local token stats from `~/.grok/sessions`.

It is a headless service. It does not add a second bar widget.

## Install

```sh
omarchy plugin add https://github.com/dougfour/omarchy-grok-usage.git --enable
```

Requires:

- Omarchy with the stock `omarchy.agents` widget enabled (default)
- Python 3 on `PATH` (stdlib only)
- Grok Build signed in (`grok login`) so weekly limits can load

Leave the built-in AI icon in the bar. After the first scan, click it and switch to **Grok**.

## Usage

- Left click the AI icon: usage panel (Claude, Codex, Fireworks, and Grok)
- `r` or Enter in the panel: refresh (Grok follows the stock update)
- Grok also refreshes about every 5 minutes, and when the stock panel rewrite of `claude.json` signals a refresh

Weekly percent comes from `https://cli-chat-proxy.grok.com/v1/billing?format=credits` using the token in `~/.grok/auth.json`. Token charts come from `~/.grok/sessions/**/updates.jsonl`.

## Remove

```sh
omarchy plugin remove io.github.dougfour.grok-usage
```

Removal deletes the plugin checkout and drops `~/.local/state/omarchy/agents/usage/grok.json`, so the Grok tab leaves the panel. It does not change `~/.grok/auth.json` or session files.

## Privacy

The collector reads local Grok session files and the saved Grok login, then calls xAI's billing endpoint. It never logs tokens. Expired tokens are not refreshed; run `grok login` (or start Grok) to renew them.

## License

MIT. See [LICENSE](LICENSE).
