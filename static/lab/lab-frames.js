/* Local-art mapping for the Crawler lab. Only indices live here; the art itself stays in the
 * git-ignored static/local_assets/ folders because its licenses forbid redistribution. */

/* Neo Zero tiles: part -> [sheet file stem, 16px frame index].
 * Parts: floor, wall, door, desk, board, rack, plant, crate, bin. Unset parts use drawn shapes. */
window.LAB_NEO_FRAMES = {};

/* Robot sheets in static/local_assets/robots/*.png. `defaults` applies to every sheet; `sheets`
 * overrides per file stem; `lanes` picks a sheet per lane (collector, discovery, automation, jev),
 * otherwise sheets are assigned in name order. Each animation is a sheet row plus column indices;
 * fps is the playback speed. `footprint` is the collision box as a fraction of the frame, anchored
 * at the sprite's feet (bottom half by default) for top-down depth sorting.
 *
 * probe_asset_1.png is produced from the operator's probe_asset_1.jpeg by
 * scripts/prepare_lab_sprite.py: 5 rows x 5 columns of 48px, transparent, feet-aligned. The source
 * labels rows 2 and 3 as west/east, but the drawings face the other way, so they are swapped here. */
window.LAB_ROBOTS = {
  defaults: {
    frameWidth: 48, frameHeight: 48, columns: 5, scale: 1,
    footprint: {width: 0.6, height: 0.5},
    animations: {
      idle:       {row: 0, frames: [0, 1, 2, 3, 4], fps: 4},
      walk_down:  {row: 1, frames: [0, 1, 2, 3, 4], fps: 8},
      walk_right: {row: 2, frames: [0, 1, 2, 3, 4], fps: 8},
      walk_left:  {row: 3, frames: [0, 1, 2, 3, 4], fps: 8},
      walk_up:    {row: 4, frames: [0, 1, 2, 3, 4], fps: 8}
    }
  },
  sheets: {},
  lanes: {}
};
