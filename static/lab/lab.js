/* Crawler lab: a top-down room where one worker per lane acts out real activity-feed entries,
 * with a console per lane underneath. Every animation is driven by an entry the worker wrote;
 * nothing here is simulated. Art: Neo Zero tileset when the operator has installed it locally,
 * otherwise a drawn fallback in the same palette. Crew sprite: Kenney 1-bit pack (CC0). */
(function(){
  "use strict";
  var mount = document.getElementById("lab-phaser");
  if (!mount) return;
  var feedUrl = mount.getAttribute("data-feed-url");
  var sheets = JSON.parse(document.getElementById("lab-sheets").textContent || "{}");
  var robotSheets = JSON.parse(document.getElementById("lab-robots").textContent || "{}");
  var ROBOTS = window.LAB_ROBOTS || {defaults: {}, sheets: {}, lanes: {}};
  var SHOW_FOOTPRINTS = /[?&]footprints=1/.test(window.location.search);

  // The room is a grid of 48px tiles. Rows 0-1 are wall; bots walk on rows 2-7.
  var T = 48, COLS = 22, ROWS = 8, W = COLS * T, H = ROWS * T, STEP_MS = 170;
  // Stations in pipeline order: discovery finds links, approval gates them, site setup teaches the
  // collector to read a new site, the collector gathers leads, and Jev optionally judges pages.
  var LANES = [
    {key: "discovery",  label: "DISCOVERY",  color: 0x3fd4ff, css: "#3fd4ff", col: 2},
    {key: "approval",   label: "APPROVAL",   color: 0xff8a3d, css: "#ff8a3d", col: 6},
    {key: "automation", label: "SITE SETUP", color: 0xff4fd8, css: "#ff4fd8", col: 10},
    {key: "collector",  label: "COLLECTOR",  color: 0x39ff9f, css: "#39ff9f", col: 14},
    {key: "jev",        label: "JEV JUDGE",  color: 0xffc94f, css: "#ffc94f", col: 18}
  ];
  var LANE = {};
  LANES.forEach(function(l){ l.x = l.col * T + T / 2; LANE[l.key] = l; });
  var DESK_ROW = 3, SEAT_ROW = 4, DOOR_TILE = {col: 20, row: 2};
  var DESK_Y = DESK_ROW * T + 20;
  var DOOR = {x: DOOR_TILE.col * T + T / 2, y: 58}, RACK = {x: W - 34, y: 6 * T}, BOARD = {x: 6 * T + T / 2, y: 50};
  var BIN_TILE = {col: 7, row: 6};  // where filtered links are dropped, next to the approval desk
  var PROPS = [[0, 6, "plant"], [2, 7, "crate"], [BIN_TILE.col, BIN_TILE.row, "bin"], [12, 7, "crate"], [17, 7, "crate"]];

  // Blocked tiles: walls, each desk (three tiles wide), the rack and the props.
  var blocked = {};
  function block(col, row){ blocked[col + "," + row] = true; }
  for (var c0 = 0; c0 < COLS; c0++){ block(c0, 0); block(c0, 1); }
  LANES.forEach(function(l){ block(l.col - 1, DESK_ROW); block(l.col, DESK_ROW); block(l.col + 1, DESK_ROW); });
  block(21, 5); block(21, 6);
  PROPS.forEach(function(p){ block(p[0], p[1]); });
  function isBlocked(col, row){ return col < 0 || row < 0 || col >= COLS || row >= ROWS || blocked[col + "," + row]; }
  function tileFeet(col, row){ return {x: col * T + T / 2, y: row * T + T - 4}; }
  function tileOf(x, feetY){ return {col: Math.floor(x / T), row: Math.floor((feetY - 1) / T)}; }

  // Breadth-first path over open tiles, 4-directional, avoiding tiles in ``occupied`` (other bots).
  // Returns the tiles to step through (start excluded); falls back to ignoring bots if boxed in.
  function findPath(from, to, occupied){
    occupied = occupied || {};
    var key = function(t){ return t.col + "," + t.row; };
    var queue = [from], came = {};
    came[key(from)] = null;
    while (queue.length){
      var cur = queue.shift();
      if (cur.col === to.col && cur.row === to.row){
        var path = [];
        for (var t = cur; key(t) !== key(from); t = came[key(t)]) path.unshift(t);
        return path;
      }
      [[1, 0], [-1, 0], [0, 1], [0, -1]].forEach(function(d){
        var next = {col: cur.col + d[0], row: cur.row + d[1]};
        if (!isBlocked(next.col, next.row) && !occupied[key(next)] && !(key(next) in came)){
          came[key(next)] = cur;
          queue.push(next);
        }
      });
    }
    return Object.keys(occupied).length ? findPath(from, to) : [];
  }

  // Robot sheet config: per-sheet overrides on top of the defaults (see lab-frames.js).
  function robotConfig(name){
    var base = ROBOTS.defaults || {}, own = (ROBOTS.sheets || {})[name] || {};
    return Object.assign({frameWidth: 48, frameHeight: 48, columns: 5, scale: 1, footprint: {width: 0.6, height: 0.5}},
                         base, own, {animations: Object.assign({}, base.animations, own.animations)});
  }
  // Animation name -> frame indices in the sheet (row-major), e.g. {idle: [0..4], walk_down: [5..9], ...}.
  function animationFrames(config){
    var out = {};
    Object.keys(config.animations).forEach(function(name){
      var anim = config.animations[name];
      out[name] = anim.frames.map(function(col){ return anim.row * config.columns + col; });
    });
    return out;
  }

  // ---------- consoles ----------
  var consoles = {};
  LANES.forEach(function(l){
    var root = document.querySelector('[data-console="' + l.key + '"]');
    consoles[l.key] = {root: root, body: root.querySelector("pre"), queue: root.querySelector("[data-queue]"),
                       dot: root.querySelector(".lab-dot"), last: 0, stick: true};
    var c = consoles[l.key];
    c.body.addEventListener("scroll", function(){
      c.stick = c.body.scrollTop + c.body.clientHeight >= c.body.scrollHeight - 8;
    });
  });
  var workerConsole = document.querySelector('[data-console="worker"] pre');

  function clock(t){
    var d = new Date(t * 1000);
    return [d.getHours(), d.getMinutes(), d.getSeconds()].map(function(n){ return ("0" + n).slice(-2); }).join(":");
  }

  // Header: what is waiting and when it unlocks. Body placeholder until the lane records activity.
  function describeQueue(c, q){
    var text, why;
    if (q.label){
      text = q.label;
      why = q.why || q.label;
    } else if (q.note){
      text = why = q.note;
    } else if (!q.queued){
      text = "queue empty";
      why = "Nothing queued for this lane right now.";
    } else if (q.ready){
      text = q.ready + " ready · " + q.queued + " queued";
      why = q.ready + " item(s) ready; waiting for the worker to reach this lane.";
    } else {
      var at = q.next_at ? clock(q.next_at).slice(0, 5) : "later";
      text = q.queued + " queued · next at " + at;
      why = q.queued + " item(s) queued but deferred until " + at + " (per-site throttle or daily request budget).";
    }
    c.queue.textContent = text;
    if (!c.last){
      if (!c.placeholder){
        c.placeholder = document.createElement("div");
        c.placeholder.className = "lab-line lab-placeholder";
        c.body.appendChild(c.placeholder);
      }
      c.placeholder.textContent = "No activity recorded yet. " + why;
    }
  }

  function appendLine(pre, entry, stick){
    var line = document.createElement("div");
    line.className = "lab-line kind-" + entry.kind;
    var ts = document.createElement("span");
    ts.className = "lab-ts";
    ts.textContent = clock(entry.t) + " ";
    var tag = document.createElement("span");
    tag.className = "lab-tag";
    tag.textContent = (entry.kind || "").toUpperCase().padEnd(6) + " ";
    var text = document.createElement("span");
    text.textContent = entry.text + (entry.ms != null ? "  " + entry.ms + "ms" : "");
    line.appendChild(ts); line.appendChild(tag); line.appendChild(text);
    pre.appendChild(line);
    while (pre.childNodes.length > 300) pre.removeChild(pre.firstChild);
    if (stick) pre.scrollTop = pre.scrollHeight;
  }

  // ---------- scene ----------
  var scene, workers = {}, focusBeam, focusLane = null, boardText, rackText, statusText, contactsToday = 0;

  function preload(){
    this.load.image("crew", mount.getAttribute("data-crew"));
    Object.keys(sheets).forEach(function(name){ scene_load_sheet(this, name); }, this);
    Object.keys(robotSheets).forEach(function(name){
      var config = robotConfig(name);
      this.load.spritesheet("bot:" + name, robotSheets[name], {frameWidth: config.frameWidth, frameHeight: config.frameHeight});
    }, this);
  }

  // Lane -> robot sheet: explicit picks from lab-frames.js, otherwise the sheets in name order.
  function robotFor(laneKey, index){
    var names = Object.keys(robotSheets).sort();
    var name = (ROBOTS.lanes || {})[laneKey] || names[index % Math.max(names.length, 1)];
    return name && scene.textures.exists("bot:" + name) ? name : null;
  }
  function registerAnimations(name){
    var config = robotConfig(name), frames = animationFrames(config), texture = "bot:" + name;
    Object.keys(frames).forEach(function(anim){
      var key = texture + ":" + anim;
      if (!scene.anims.exists(key)){
        scene.anims.create({key: key, frames: scene.anims.generateFrameNumbers(texture, {frames: frames[anim]}),
                            frameRate: config.animations[anim].fps || (anim === "idle" ? 4 : 8), repeat: -1});
      }
    });
    return config;
  }
  function play(w, anim){
    if (w.robot && scene.anims.exists("bot:" + w.robot + ":" + anim)) w.sprite.play("bot:" + w.robot + ":" + anim, true);
  }

  // Collision box: the bottom half of the sprite, anchored at its feet. It decides who is in front
  // (depth = feet line) and which tiles a bot can occupy, giving the top-down perspective.
  function footprint(w, x, feetY){
    var width = w.sprite.displayWidth * w.foot.width, height = w.sprite.displayHeight * w.foot.height;
    x = x == null ? w.sprite.x : x;
    feetY = feetY == null ? w.sprite.y : feetY;
    return new Phaser.Geom.Rectangle(x - width / 2, feetY - height, width, height);
  }
  function footprintBlocked(w, x, feetY){
    var box = footprint(w, x, feetY);
    var corners = [[box.left, box.top], [box.right - 1, box.top], [box.left, box.bottom - 1], [box.right - 1, box.bottom - 1]];
    return corners.some(function(p){ return isBlocked(Math.floor(p[0] / T), Math.floor(p[1] / T)); });
  }
  function bumpsAnotherBot(w, x, feetY){
    var box = footprint(w, x, feetY);
    return Object.keys(workers).some(function(k){
      var other = workers[k];
      return other !== w && Phaser.Geom.Intersects.RectangleToRectangle(box, footprint(other));
    });
  }
  function syncDepth(w){
    w.sprite.setDepth(w.sprite.y);
    w.ring.setPosition(w.sprite.x, w.sprite.y - 3).setDepth(w.sprite.y - 1);
    if (w.debug){
      var box = footprint(w);
      w.debug.setPosition(box.x, box.y).setSize(box.width, box.height);
    }
  }
  function scene_load_sheet(s, name){
    s.load.spritesheet("neo:" + name, sheets[name], {frameWidth: 16, frameHeight: 16});
  }

  // Frame picks for the Neo Zero sheets, filled in once the operator's local copy has been inspected.
  // Each entry: [sheet name, frame index]. Missing entries fall back to drawn shapes.
  var NEO = window.LAB_NEO_FRAMES || {};
  function neo(part){
    var pick = NEO[part];
    return pick && scene.textures.exists("neo:" + pick[0]) ? pick : null;
  }
  function tile(part, x, y, scale){
    var pick = neo(part);
    if (!pick) return null;
    return scene.add.image(x, y, "neo:" + pick[0], pick[1]).setScale(scale || 3);
  }

  function drawRoom(){
    var g = scene.add.graphics();
    // Floor
    for (var ty = 0; ty < H; ty += 48){
      for (var tx = 0; tx < W; tx += 48){
        if (!tile(ty < 96 ? "wall" : "floor", tx + 24, ty + 24)){
          g.fillStyle(ty < 96 ? 0x141a33 : ((tx + ty) / 48 % 2 ? 0x0d1224 : 0x0f152a), 1);
          g.fillRect(tx, ty, 48, 48);
        }
      }
    }
    g.lineStyle(1, 0x1f2a4d, 0.6);
    for (var gx = 0; gx <= W; gx += 48) g.lineBetween(gx, 96, gx, H);
    for (var gy = 96; gy <= H; gy += 48) g.lineBetween(0, gy, W, gy);
    // Wall trim with neon strip
    g.fillStyle(0x0a0e1c, 1).fillRect(0, 90, W, 6);
    g.fillStyle(0xff4fd8, 0.8).fillRect(0, 94, W, 2);

    // Uplink door (to the public web)
    if (!tile("door", DOOR.x, DOOR.y)){
      g.fillStyle(0x050810, 1).fillRect(DOOR.x - 30, DOOR.y - 36, 60, 64);
      g.lineStyle(3, 0x3fd4ff, 1).strokeRect(DOOR.x - 30, DOOR.y - 36, 60, 64);
    }
    label(DOOR.x - 60, DOOR.y - 44, "UPLINK · PUBLIC WEB", "#3fd4ff");

    // Approvals board
    if (!tile("board", BOARD.x, BOARD.y)){
      g.fillStyle(0x10203a, 1).fillRect(BOARD.x - 48, BOARD.y - 28, 96, 56);
      g.lineStyle(2, 0xffc94f, 1).strokeRect(BOARD.x - 48, BOARD.y - 28, 96, 56);
    }
    label(BOARD.x, BOARD.y - 38, "NEEDS REVIEW", "#ffc94f");
    boardText = scene.add.text(BOARD.x, BOARD.y, "–", mono(22, "#ffe7a8")).setOrigin(0.5);

    // Server rack (the database) where accepted contacts land
    if (!tile("rack", RACK.x, RACK.y)){
      g.fillStyle(0x0b0f1d, 1).fillRect(RACK.x - 26, RACK.y - 60, 52, 100);
      g.lineStyle(2, 0x39ff9f, 0.9).strokeRect(RACK.x - 26, RACK.y - 60, 52, 100);
      for (var i = 0; i < 6; i++) g.fillStyle(i % 2 ? 0x39ff9f : 0x1d6e4a, 1).fillRect(RACK.x - 18, RACK.y - 50 + i * 15, 8, 4);
    }
    label(RACK.x, RACK.y - 70, "LEAD DB", "#39ff9f");
    rackText = scene.add.text(RACK.x - 40, RACK.y + 56, "", mono(11, "#9dffcf")).setOrigin(0.5);

    // Props for texture
    PROPS.forEach(function(p){
      var x = p[0] * T + T / 2, y = p[1] * T + T / 2;
      if (!tile(p[2], x, y)){
        g.fillStyle(p[2] === "plant" ? 0x1d6e4a : 0x2a2f4f, 1).fillRect(x - 14, y - 14, 28, 28);
      }
    });
    statusText = scene.add.text(12, H - 20, "", mono(11, "#8c9ac2")).setDepth(3000);
  }

  function mono(size, color){
    return {fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace", fontSize: size + "px", color: color};
  }
  function label(x, y, text, color){
    return scene.add.text(x, y, text, mono(10, color)).setOrigin(0.5).setAlpha(0.9);
  }

  function drawStation(l, index){
    var g = scene.add.graphics().setDepth((DESK_ROW + 1) * T - 6);
    if (!tile("desk", l.x, DESK_Y)){
      g.fillStyle(0x1b2140, 1).fillRect(l.x - 64, DESK_Y - 14, 128, 34);
      g.fillStyle(0x2a3160, 1).fillRect(l.x - 64, DESK_Y - 14, 128, 6);
    }
    var screen = scene.add.rectangle(l.x, DESK_Y - 22, 58, 30, 0x050810).setStrokeStyle(2, l.color, 0.5).setDepth(g.depth + 1);
    var glow = scene.add.rectangle(l.x, DESK_Y - 22, 50, 22, l.color, 0.08).setDepth(g.depth + 2);
    label(l.x, (SEAT_ROW + 1) * T + 8, l.label, l.css).setDepth(1000);
    var name = robotFor(l.key, index), config = name ? registerAnimations(name) : null, sprite;
    var home = tileFeet(l.col, SEAT_ROW);
    if (name){
      sprite = scene.add.sprite(home.x, home.y, "bot:" + name).setOrigin(0.5, 1).setScale(config.scale);
    } else {
      sprite = scene.add.image(home.x, home.y, "crew").setOrigin(0.5, 1).setDisplaySize(40, 40).setTint(l.color);
    }
    // A lane-coloured floor ring tells identical bots apart without tinting their art.
    var ring = scene.add.ellipse(home.x, home.y - 3, 34, 10).setStrokeStyle(2, l.color, 0.8);
    var bubble = scene.add.text(home.x, home.y, "", mono(10, "#e8ecff"))
      .setOrigin(0.5, 1).setBackgroundColor("#0b1020").setPadding(4, 2, 4, 2).setDepth(2000).setVisible(false);
    var zz = scene.add.text(home.x + 16, home.y - 52, "z", mono(12, "#5b6690")).setDepth(1999);
    scene.tweens.add({targets: zz, y: "-=12", alpha: {from: 1, to: 0}, duration: 1600, repeat: -1});
    var w = {lane: l, sprite: sprite, robot: name, ring: ring, bubble: bubble, zz: zz, screen: screen, glow: glow,
             foot: config ? config.footprint : {width: 0.6, height: 0.5}, queue: [], busy: false, lastActive: 0,
             home: {col: l.col, row: SEAT_ROW}};
    if (SHOW_FOOTPRINTS) w.debug = scene.add.rectangle(0, 0, 1, 1).setOrigin(0).setStrokeStyle(1, 0xff0000).setDepth(3000);
    workers[l.key] = w;
    play(w, "idle");
    syncDepth(w);
  }

  function say(w, text, color, ms){
    w.bubble.setText(text.length > 34 ? text.slice(0, 33) + "…" : text).setColor(color || "#e8ecff")
      .setPosition(w.sprite.x, w.sprite.y - w.sprite.displayHeight - 4).setVisible(true);
    clearTimeout(w.bubbleTimer);
    w.bubbleTimer = setTimeout(function(){ w.bubble.setVisible(false); }, ms || 2200);
  }

  function wake(w){
    w.lastActive = Date.now();
    w.zz.setVisible(false);
    w.glow.setFillStyle(w.lane.color, 0.35);
    w.screen.setStrokeStyle(2, w.lane.color, 1);
  }

  // Grid movement: one tile per step along a BFS path, facing and animating in the step's direction.
  // A step whose footprint would overlap another bot waits briefly; after a few tries it proceeds.
  function walkTo(w, target, done){
    var occupied = {};
    Object.keys(workers).forEach(function(k){
      var other = workers[k];
      if (other === w) return;
      var at = tileOf(other.sprite.x, other.sprite.y);
      occupied[at.col + "," + at.row] = true;
    });
    var path = findPath(tileOf(w.sprite.x, w.sprite.y), target, occupied);
    var bob = w.robot ? null : scene.tweens.add({targets: w.sprite, angle: {from: -7, to: 7}, yoyo: true, repeat: -1, duration: 110});
    var step = function(i, waited){
      if (i >= path.length){
        if (bob){ bob.stop(); w.sprite.angle = 0; }
        play(w, "idle");
        done && done();
        return;
      }
      var to = tileFeet(path[i].col, path[i].row), dx = to.x - w.sprite.x, dy = to.y - w.sprite.y;
      if (footprintBlocked(w, to.x, to.y)){ step(path.length); return; }  // map changed under us; stop here
      if (waited < 6 && bumpsAnotherBot(w, to.x, to.y)){
        play(w, "idle");
        scene.time.delayedCall(150, function(){ step(i, waited + 1); });
        return;
      }
      play(w, Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? "walk_right" : "walk_left") : (dy > 0 ? "walk_down" : "walk_up"));
      scene.tweens.add({targets: w.sprite, x: to.x, y: to.y, duration: STEP_MS, ease: "Linear",
        onUpdate: function(){ syncDepth(w); },
        onComplete: function(){ syncDepth(w); step(i + 1, 0); }});
    };
    step(0, 0);
  }

  function packet(color, ok, done){
    var p = scene.add.rectangle(DOOR.x, DOOR.y, 8, 8, color).setDepth(2500);
    scene.tweens.add({targets: p, x: W + 10, duration: 260, ease: "Quad.easeIn", onComplete: function(){
      p.setFillStyle(ok ? 0x39ff9f : 0xff5a5a);
      scene.tweens.add({targets: p, x: DOOR.x, duration: 260, ease: "Quad.easeOut", onComplete: function(){ p.destroy(); done && done(); }});
    }});
  }

  function flyToken(from, to, color, then){
    var token = scene.add.rectangle(from.x, from.y - 30, 10, 12, color).setStrokeStyle(1, 0xffffff, 0.6).setDepth(2500);
    scene.tweens.add({targets: token, x: to.x, y: to.y, duration: 650, ease: "Sine.easeInOut",
      onComplete: function(){ token.destroy(); then && then(); }});
  }
  // An approved source travels as a ticket from the approval desk to the next station.
  function handOff(from, to, color, then){
    flyToken(from.sprite, {x: to.sprite.x, y: to.sprite.y - 30}, color, then);
  }

  function toRack(from, count){
    for (var i = 0; i < Math.min(count, 8); i++){
      (function(delay){
        var c = scene.add.star(from.x, from.y - 30, 5, 3, 7, 0x39ff9f).setDepth(2500);
        scene.tweens.add({targets: c, x: RACK.x, y: RACK.y - 20, delay: delay, duration: 700, ease: "Sine.easeInOut",
          onComplete: function(){ c.destroy(); }});
      })(i * 90);
    }
  }

  // One entry -> one short act. Runs per lane, in order; a long backlog skips walking.
  function perform(w, e, hurry, done){
    wake(w);
    var ok = e.status && e.status < 300;
    switch (e.kind){
      case "fetch":
      case "error":
        if (hurry){
          packet(w.lane.color, e.kind === "fetch" && ok, done);
          say(w, e.kind === "error" ? "error" : String(e.status), e.kind === "error" ? "#ff8a8a" : "#9dffcf", 900);
          return;
        }
        walkTo(w, DOOR_TILE, function(){
          play(w, "walk_up");
          packet(w.lane.color, e.kind === "fetch" && ok, function(){
            say(w, e.kind === "error" ? "✗ " + e.text.replace(/^GET \S+ -> /, "") : "✓ " + e.status + " " + Math.round((e.bytes || 0) / 1024) + "KB",
                e.kind === "error" ? "#ff8a8a" : "#9dffcf");
            walkTo(w, w.home, done);
          });
        });
        return;
      case "pick":
        say(w, "▶ " + e.text, w.lane.css);
        scene.tweens.add({targets: w.sprite, y: "-=6", yoyo: true, duration: 140, onComplete: done});
        return;
      case "approved":
        say(w, "✓ approved" + (e.by === "operator" ? " by you" : " by policy"), "#ffc08a");
        var next = /collector/.test(e.text) ? workers.collector : workers.automation;
        handOff(w, next || w, w.lane.color, function(){ if (next) wake(next); });
        scene.time.delayedCall(hurry ? 150 : 700, done);
        return;
      case "filtered":
      case "dismissed":
        say(w, (e.kind === "dismissed" ? "✗ dismissed" : "✗ filtered: ") + (e.kind === "filtered" ? e.text.split(": ").pop() : ""), "#8c9ac2", 1600);
        var bin = tileFeet(BIN_TILE.col, BIN_TILE.row);
        flyToken(w.sprite, {x: bin.x, y: bin.y - 20}, 0x5b6690);
        scene.time.delayedCall(hurry ? 80 : 450, done);
        return;
      case "pending":
        say(w, "? needs your review", "#ffc94f", 1600);
        flyToken(w.sprite, {x: BOARD.x, y: BOARD.y}, 0xffc94f);
        scene.time.delayedCall(hurry ? 80 : 450, done);
        return;
      case "result":
        say(w, e.contacts ? "★ " + e.contacts + " contact(s)" : "done", e.contacts ? "#9dffcf" : "#c8cfe8");
        if (e.contacts){ toRack(w.sprite, e.contacts); contactsToday += e.contacts; }
        scene.time.delayedCall(hurry ? 150 : 600, done);
        return;
      case "blocked":
        say(w, "⛔ blocked — stopped, not retried", "#ff8a8a", 2600);
        scene.tweens.add({targets: w.sprite, x: "+=4", yoyo: true, repeat: 3, duration: 60});
        scene.time.delayedCall(hurry ? 150 : 900, done);
        return;
      case "wait":
        say(w, "⧗ throttled", "#8c9ac2", 1400);
        scene.time.delayedCall(hurry ? 100 : 500, done);
        return;
      case "robots":
        say(w, "reading robots.txt", "#c8cfe8", 1400);
        scene.time.delayedCall(hurry ? 100 : 500, done);
        return;
      default:
        say(w, e.text, e.kind === "failed" ? "#ff8a8a" : "#e8ecff");
        scene.time.delayedCall(hurry ? 150 : 700, done);
    }
  }

  function pump(w){
    if (w.busy || !w.queue.length) return;
    w.busy = true;
    var e = w.queue.shift();
    perform(w, e, w.queue.length > 4, function(){ w.busy = false; pump(w); });
  }

  function setFocus(laneKey){
    if (focusLane === laneKey) return;
    focusLane = laneKey;
    var l = LANE[laneKey];
    if (!l) return;
    scene.tweens.add({targets: focusBeam, x: l.x, duration: 400, ease: "Sine.easeInOut"});
    focusBeam.setFillStyle(l.color, 0.12);
  }

  function create(){
    scene = this;
    drawRoom();
    focusBeam = scene.add.rectangle(LANES[0].x, 2 * T + 3 * T, 4 * T - 12, 6 * T, 0xffffff, 0).setDepth(1);
    LANES.forEach(drawStation);
    if (SHOW_FOOTPRINTS) mount.labDebug = {workers: workers, findPath: findPath};  // ?footprints=1 inspection aid
    scene.time.addEvent({delay: 1000, loop: true, callback: function(){
      var now = Date.now();
      Object.keys(workers).forEach(function(k){
        var w = workers[k];
        if (now - w.lastActive > 15000 && !w.busy){
          w.zz.setVisible(true);
          w.glow.setFillStyle(w.lane.color, 0.08);
          w.screen.setStrokeStyle(2, w.lane.color, 0.5);
        }
      });
    }});
    poll();
    setInterval(poll, 1500);
  }

  // ---------- polling ----------
  var after = 0, first = true, idleNote = null, onlineEl = document.getElementById("lab-online"), approvalsEl = document.getElementById("lab-approvals"),
      reviewLink = document.getElementById("lab-review"), reviewRest = document.getElementById("lab-review-rest");

  function poll(){
    fetch(feedUrl + "?after=" + after, {headers: {"Accept": "application/json"}, credentials: "same-origin"})
      .then(function(r){ return r.json(); })
      .then(function(data){
        onlineEl.classList.toggle("online", data.worker_online);
        onlineEl.nextElementSibling.textContent = data.worker_online ? "Worker online" : "Worker offline";
        var review = data.review;
        approvalsEl.textContent = review.worth_a_look;
        reviewLink.href = data.review_href;
        reviewRest.textContent = "(" + review.low_signal + " low-signal · " + review.auto_filtered + " auto-filtered)";
        boardText.setText(String(review.worth_a_look));
        LANES.forEach(function(l){ describeQueue(consoles[l.key], data.queues[l.key]); });
        var now = Date.now() / 1000;
        data.entries.forEach(function(e){
          after = Math.max(after, e.t);
          var lane = LANE[e.lane] ? e.lane : null;
          appendLine(workerConsole, e, true);
          if (e.kind === "idle"){ idleNote = e; return; }
          idleNote = null;
          if (!lane) return;
          var c = consoles[lane];
          if (c.placeholder){ c.placeholder.remove(); c.placeholder = null; }
          appendLine(c.body, e, c.stick);
          c.last = e.t;
          // On first load, show history in the consoles but only act out the last few seconds.
          if (!first || now - e.t < 10){
            workers[lane].queue.push(e);
            pump(workers[lane]);
          }
          setFocus(lane);
        });
        LANES.forEach(function(l){
          consoles[l.key].dot.classList.toggle("online", now - consoles[l.key].last < 20);
        });
        rackText.setText(contactsToday ? "+" + contactsToday + " this session" : "");
        statusText.setText(idleNote && now - idleNote.t < 180 ? idleNote.text
          : "one worker process · time-slices lanes · focus: " + (focusLane ? LANE[focusLane].label : "—"));
        first = false;
      })
      .catch(function(){ /* keep the scene; next poll retries */ });
  }

  new Phaser.Game({type: Phaser.AUTO, width: W, height: H, parent: "lab-phaser", backgroundColor: "#070a14",
                   pixelArt: true, scene: {preload: preload, create: create}});
})();
