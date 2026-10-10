% Choroidal thickness from OCT (Figure 3).
S = load('../data/choroid_oct.mat');
delta = S.thickness_um(:,2) - S.thickness_um(:,1);
isBlue = strcmp(S.group, 'blue');
fprintf('blue %.1f um, control %.1f um\n', mean(delta(isBlue)), mean(delta(~isBlue)));
[~, p] = ttest2(delta(isBlue), delta(~isBlue));
fprintf('p = %.3f\n', p);
figure; bar([mean(delta(isBlue)) mean(delta(~isBlue))]);
set(gca, 'XTickLabel', {'Blue', 'Control'}); ylabel('\Delta choroid (\mum)');
exportgraphics(gcf, '../manuscript/figures/fig3_choroid.png');
