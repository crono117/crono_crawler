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

  var LANES = [
    {key: "collector",  label: "COLLECTOR",  color: 0x39ff9f, css: "#39ff9f", x: 200},
    {key: "discovery",  label: "DISCOVERY",  color: 0x3fd4ff, css: "#3fd4ff", x: 380},
    {key: "automation", label: "SITE SETUP", color: 0xff4fd8, css: "#ff4fd8", x: 560},
    {key: "jev",        label: "JEV JUDGE",  color: 0xffc94f, css: "#ffc94f", x: 740}
  ];
  var LANE = {};
  LANES.forEach(function(l){ LANE[l.key] = l; });
  var W = 960, H = 384, DESK_Y = 176, SEAT_Y = 232, AISLE_Y = 112;
  var DOOR = {x: 892, y: 70}, RACK = {x: 900, y: 300}, BOARD = {x: 72, y: 64};

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
      g.fillStyle(0x050810, 1).fillRect(DOOR.x - 30, DOOR.y - 44, 60, 70);
      g.lineStyle(3, 0x3fd4ff, 1).strokeRect(DOOR.x - 30, DOOR.y - 44, 60, 70);
    }
    label(DOOR.x, DOOR.y - 58, "UPLINK · PUBLIC WEB", "#3fd4ff");

    // Approvals board
    if (!tile("board", BOARD.x, BOARD.y)){
      g.fillStyle(0x10203a, 1).fillRect(BOARD.x - 48, BOARD.y - 40, 96, 64);
      g.lineStyle(2, 0xffc94f, 1).strokeRect(BOARD.x - 48, BOARD.y - 40, 96, 64);
    }
    label(BOARD.x, BOARD.y - 52, "APPROVAL QUEUE", "#ffc94f");
    boardText = scene.add.text(BOARD.x, BOARD.y - 8, "–", mono(22, "#ffe7a8")).setOrigin(0.5);

    // Server rack (the database) where accepted contacts land
    if (!tile("rack", RACK.x, RACK.y)){
      g.fillStyle(0x0b0f1d, 1).fillRect(RACK.x - 26, RACK.y - 60, 52, 100);
      g.lineStyle(2, 0x39ff9f, 0.9).strokeRect(RACK.x - 26, RACK.y - 60, 52, 100);
      for (var i = 0; i < 6; i++) g.fillStyle(i % 2 ? 0x39ff9f : 0x1d6e4a, 1).fillRect(RACK.x - 18, RACK.y - 50 + i * 15, 8, 4);
    }
    label(RACK.x, RACK.y - 74, "LEAD DB", "#39ff9f");
    rackText = scene.add.text(RACK.x, RACK.y + 50, "", mono(11, "#9dffcf")).setOrigin(0.5);

    // Props for texture
    [[40, 330, "plant"], [120, 340, "crate"], [470, 350, "crate"], [650, 350, "bin"]].forEach(function(p){
      if (!tile(p[2], p[0], p[1])){
        g.fillStyle(p[2] === "plant" ? 0x1d6e4a : 0x2a2f4f, 1).fillRect(p[0] - 14, p[1] - 14, 28, 28);
      }
    });
    statusText = scene.add.text(12, H - 20, "", mono(11, "#8c9ac2"));
  }

  function mono(size, color){
    return {fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace", fontSize: size + "px", color: color};
  }
  function label(x, y, text, color){
    return scene.add.text(x, y, text, mono(10, color)).setOrigin(0.5).setAlpha(0.9);
  }

  function drawStation(l){
    var g = scene.add.graphics();
    if (!tile("desk", l.x, DESK_Y)){
      g.fillStyle(0x1b2140, 1).fillRect(l.x - 56, DESK_Y - 18, 112, 36);
      g.fillStyle(0x2a3160, 1).fillRect(l.x - 56, DESK_Y - 18, 112, 6);
    }
    var screen = scene.add.rectangle(l.x, DESK_Y - 30, 58, 34, 0x050810).setStrokeStyle(2, l.color, 0.5);
    var glow = scene.add.rectangle(l.x, DESK_Y - 30, 50, 26, l.color, 0.08);
    label(l.x, DESK_Y + 30, l.label, l.css);
    var sprite = scene.add.image(l.x, SEAT_Y, "crew").setDisplaySize(40, 40).setTint(l.color);
    var bubble = scene.add.text(l.x, SEAT_Y - 44, "", mono(10, "#e8ecff"))
      .setOrigin(0.5).setBackgroundColor("#0b1020").setPadding(4, 2, 4, 2).setDepth(20).setVisible(false);
    var zz = scene.add.text(l.x + 18, SEAT_Y - 30, "z", mono(12, "#5b6690")).setDepth(19);
    scene.tweens.add({targets: zz, y: "-=12", alpha: {from: 1, to: 0}, duration: 1600, repeat: -1});
    workers[l.key] = {lane: l, sprite: sprite, bubble: bubble, zz: zz, screen: screen, glow: glow,
                      queue: [], busy: false, lastActive: 0, home: {x: l.x, y: SEAT_Y}};
  }

  function say(w, text, color, ms){
    w.bubble.setText(text.length > 34 ? text.slice(0, 33) + "…" : text).setColor(color || "#e8ecff")
      .setPosition(w.sprite.x, w.sprite.y - 44).setVisible(true);
    clearTimeout(w.bubbleTimer);
    w.bubbleTimer = setTimeout(function(){ w.bubble.setVisible(false); }, ms || 2200);
  }

  function wake(w){
    w.lastActive = Date.now();
    w.zz.setVisible(false);
    w.glow.setFillStyle(w.lane.color, 0.35);
    w.screen.setStrokeStyle(2, w.lane.color, 1);
  }

  function walk(w, points, done){
    var chain = points.map(function(p){
      return {targets: [w.sprite], x: p.x, y: p.y, duration: p.d || 380, ease: "Linear"};
    });
    var bob = scene.tweens.add({targets: w.sprite, angle: {from: -7, to: 7}, yoyo: true, repeat: -1, duration: 110});
    var step = function(i){
      if (i >= chain.length){ bob.stop(); w.sprite.angle = 0; done && done(); return; }
      var c = chain[i];
      c.onComplete = function(){ step(i + 1); };
      scene.tweens.add(c);
    };
    step(0);
  }

  function packet(color, ok, done){
    var p = scene.add.rectangle(DOOR.x, DOOR.y, 8, 8, color).setDepth(15);
    scene.tweens.add({targets: p, x: W + 10, duration: 260, ease: "Quad.easeIn", onComplete: function(){
      p.setFillStyle(ok ? 0x39ff9f : 0xff5a5a);
      scene.tweens.add({targets: p, x: DOOR.x, duration: 260, ease: "Quad.easeOut", onComplete: function(){ p.destroy(); done && done(); }});
    }});
  }

  function toRack(from, count){
    for (var i = 0; i < Math.min(count, 8); i++){
      (function(delay){
        var c = scene.add.star(from.x, from.y - 20, 5, 3, 7, 0x39ff9f).setDepth(16);
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
        var home = w.home, side = home.x + 70;
        walk(w, [{x: side, y: home.y, d: 200}, {x: side, y: AISLE_Y, d: 300}, {x: DOOR.x - 40, y: AISLE_Y, d: 420}], function(){
          packet(w.lane.color, e.kind === "fetch" && ok, function(){
            say(w, e.kind === "error" ? "✗ " + e.text.replace(/^GET \S+ -> /, "") : "✓ " + e.status + " " + Math.round((e.bytes || 0) / 1024) + "KB",
                e.kind === "error" ? "#ff8a8a" : "#9dffcf");
            walk(w, [{x: side, y: AISLE_Y, d: 420}, {x: side, y: home.y, d: 300}, {x: home.x, y: home.y, d: 200}], done);
          });
        });
        return;
      case "pick":
        say(w, "▶ " + e.text, w.lane.css);
        scene.tweens.add({targets: w.sprite, y: w.home.y - 6, yoyo: true, duration: 140, onComplete: done});
        return;
      case "result":
        say(w, e.contacts ? "★ " + e.contacts + " contact(s)" : "done", e.contacts ? "#9dffcf" : "#c8cfe8");
        if (e.contacts){ toRack(w.sprite, e.contacts); contactsToday += e.contacts; }
        scene.time.delayedCall(hurry ? 150 : 600, done);
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
    focusBeam = scene.add.rectangle(LANES[0].x, 240, 150, 288, 0xffffff, 0).setDepth(-1);
    LANES.forEach(drawStation);
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
  var after = 0, first = true, idleNote = null, onlineEl = document.getElementById("lab-online"), approvalsEl = document.getElementById("lab-approvals");

  function poll(){
    fetch(feedUrl + "?after=" + after, {headers: {"Accept": "application/json"}, credentials: "same-origin"})
      .then(function(r){ return r.json(); })
      .then(function(data){
        onlineEl.classList.toggle("online", data.worker_online);
        onlineEl.nextElementSibling.textContent = data.worker_online ? "Worker online" : "Worker offline";
        approvalsEl.textContent = data.approvals;
        boardText.setText(String(data.approvals));
        LANES.forEach(function(l){
          consoles[l.key].queue.textContent = data.queues[l.key] + " queued";
        });
        var now = Date.now() / 1000;
        data.entries.forEach(function(e){
          after = Math.max(after, e.t);
          var lane = LANE[e.lane] ? e.lane : null;
          appendLine(workerConsole, e, true);
          if (e.kind === "idle"){ idleNote = e; return; }
          idleNote = null;
          if (!lane) return;
          var c = consoles[lane];
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
