## Detached Mode

Each step is a separate session with no shared context. Rebuild only from
context permitted by the current commission. Where allowed, use your **radio
private channel** (`fractal radio read --channel=private`), **memory**
(`$MEMORY_DIR`), and recent **saved messages**
(`fractal radio messages --saved`). Do not open sealed or excluded channels; use
permitted on-disk continuation instead. Preserve the frozen subject and other
read boundaries. Before finishing, write a concise handoff at the commissioned
private path, or use radio when authorized:

```bash
fractal radio send "<context>" --node=$CURRENT_BRANCH --channel=private --subject="<subject>" --priority=<0-10>
```
