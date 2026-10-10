# Statistics reported in the Results.
d <- read.csv("../data/chick_biometry.csv")
d$dR <- d$refraction_d7 - d$refraction_d0
d$dAL <- d$axial_d7 - d$axial_d0

# refraction (all eyes)
t.test(dR ~ group, data = d)
# axial length (all eyes)
t.test(dAL ~ group, data = d)

# bootstrap CI for the refraction difference
boot <- replicate(2000, {
  b <- d[sample(nrow(d), replace = TRUE), ]
  diff(tapply(b$dR, b$group, mean, na.rm = TRUE))
})
quantile(boot, c(0.025, 0.975))
