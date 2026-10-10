# Demo paper

A short, deliberately flawed paper laid out like a real revision folder:

```
manuscript/main.tex, refs.bib, figures/   the paper (Figure 3's image is intentionally missing)
submitted.pdf                             the line-numbered PDF the "reviewers" read
reviews/decision-letter.txt               the decision letter
code/make_figures.R, stats.R              R code for Figures 1-2 and the statistics
code/choroid_thickness.m                  MATLAB code for Figure 3
data/chick_biometry.csv, choroid_oct.mat  the raw data
```

Planted problems include:

- **The paper**: pseudo-replication, unmasked measurement, overclaiming, `p = 0.000`.
- **Figures vs code**: Figure 1's caption says SD but the R code plots SEM;
  Figure 2's caption says "lens-wearing eyes, n = 12" but the code plots both
  eyes and drops chick 7.
- **The code**: `setwd()`, no `set.seed()`, a duplicate data row.
- **The reviews**, which contain the classics: a comment that misreads the
  paper (the OCT method *is* described, at lines 27-31), an incomprehensible
  one, a hostile one, line-number references, and two reviewers asking for
  opposite things.

```bash
python papr.py review --dir examples/demo-paper --backend ollama --depth standard
```
