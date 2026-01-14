# Playbooks

## Common workflows (add sections as they emerge)
### Add a new status service (Statuspage)
- Add an entry to `servicedash.json` with `type: "statuspage"` and `base_url: "https://<status-site>"`.
- (Optional) Track a component: `type: "statuspage_component"` with `component_match: ["substring"]`.
- Run `python -m servicedash poll --once --log` then `python -m servicedash run`.

### Add a new market quote (Stooq)
- Add `type: "stooq_quote"` with `symbol` set to the Stooq symbol (e.g. `tsla.us`, `xauusd`, `eurusd`).
- (Optional) Add `format` to control decimals/prefix/suffix.
