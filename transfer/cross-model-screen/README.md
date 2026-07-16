# Cross-Model Orchestration Transfer Screen

This is a fixed, matched screen of orchestration methods discovered with Luna Light. It compares their existing Luna Light results with new Terra Low and Luna Medium runs on the same 12 historical cases.

The screen is deliberately compact:

- 12 cases, balanced four per family
- five shared base answers per model condition
- one-review repair, three-review repair, and sequential cross-examination
- 144 maximum calls per new model condition
- 288 maximum new calls total
- no adaptive strategy changes
- no external Python packages
- an answer-blind, reproducible hash selection from six eligible historical panels

The historical Luna Light results are a secondary, non-contemporaneous reference. The clean primary comparison is the interleaved Terra Low versus Luna Medium replay. Because these are historical cases from the same task distribution used during orchestration research, this screen measures model transfer, not independent domain generalization.

Run:

```bash
python3 run_transfer.py prepare
python3 run_transfer.py run
python3 run_transfer.py status
```

Only infrastructure failures may be retried. Accuracy remains unscored until all registered stages finish.
