/**
 * The seam — where the Tide (Arms, hour of day) meets the Usage bands hanging below it (Users, last 30
 * days). The two drawings overlap by SEAM px: the Tide's foot fades out over the top of the gold, so the
 * colour blends instead of butting. Axis labels are knocked out of the solid colour on either side —
 * hours just above the overlap, dates just below — at the same x positions, so the columns line up even
 * though the axes differ. Ticks stay in the solid middle: the side fades would eat a knockout at the edges,
 * and the legend floats over the right one.
 */
export const SEAM = 44;
/** Each side's fade covers this share of the overlap: the gold fades in over the top part, the Tide fades
 *  out over the bottom part. Staggered (not both across the whole overlap) so the middle never dips dark,
 *  and both soft so neither drawing's edge shows as a line where the side fades thin the other one. */
export const SEAM_FADE = 0.65;
/** Right strip kept clear of both drawings, for the legend (fraction of the width). */
export const LEGEND_W = 0.12;
export const SEAM_TICKS = [1 / 6, 2 / 6, 3 / 6, 4 / 6];
/** Knockout label size/weight — heavier than the old ink labels, since a hole reads thinner than ink. */
export const SEAM_FONT = { size: 12, weight: 600 };
