/* Local-art mapping for the Crawler lab. Only indices live here; the art itself stays in the
 * git-ignored static/local_assets/ folders because its licenses forbid redistribution. */

/* Neo Zero tiles: part -> [sheet file stem, 16px frame index].
 * Parts: floor, wall, door, desk, board, rack, plant, crate, bin. Unset parts use drawn shapes. */
window.LAB_NEO_FRAMES = {};

/* Sci-fi robot sheets in static/local_assets/robots/. Defaults assume one row per direction and
 * idle frames followed by walk frames; adjust after inspecting the pack. lanes maps a lane
 * (collector, discovery, automation, jev) to a sheet file stem; unset lanes take sheets in name order. */
window.LAB_ROBOTS = {
  frameWidth: 16, frameHeight: 16, columns: 6,
  rows: {down: 0, left: 1, right: 2, up: 3},
  idle: [0, 1], walk: [2, 3, 4, 5],
  lanes: {}
};
