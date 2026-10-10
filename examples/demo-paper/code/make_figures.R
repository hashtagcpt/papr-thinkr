# Figures 1 and 2 for the manuscript.
library(ggplot2)
setwd("C:/Users/lab/Dropbox/blue_light_chicks/code")

d <- read.csv("../data/chick_biometry.csv")
d$dR <- d$refraction_d7 - d$refraction_d0
d$dAL <- d$axial_d7 - d$axial_d0
lens <- subset(d, eye == "lens")

# ---- Figure 1: change in refraction, lens-wearing eyes -----------------------
s <- aggregate(dR ~ group, data = lens, FUN = function(x) c(m = mean(x), se = sd(x) / sqrt(length(x))))
s <- do.call(data.frame, s)
p1 <- ggplot(s, aes(group, dR.m, fill = group)) +
  geom_col(width = 0.6) +
  geom_errorbar(aes(ymin = dR.m - dR.se, ymax = dR.m + dR.se), width = 0.2) +
  scale_fill_manual(values = c(blue = "#2b6cb0", control = "#c53030")) +
  labs(x = NULL, y = "Change in refraction (D)") +
  theme_classic()
ggsave("../manuscript/figures/fig1_refraction.pdf", p1, width = 3.2, height = 3)

# ---- Figure 2: axial elongation, both eyes ------------------------------------
lens2 <- subset(d, !is.na(refraction_d7))
png("../manuscript/figures/fig2_axial.png", width = 900, height = 700, res = 150)
boxplot(dAL ~ group + eye, data = lens2, col = c("red", "green"),
        ylab = "Axial elongation (mm)", xlab = "")
dev.off()
