// Deterministic choreography: seconds in, a complete pose out. No DOM or timers.
export const SCENE_SECONDS = 8;
export const DURATION = 48;
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
export const ease = v => { v = clamp(v, 0, 1); return v * v * (3 - 2 * v); };
const mix = (a, b, t) => a + (b - a) * t;
function between(t, a, b) { return ease((t - a) / (b - a)); }
function keys(t, frames) {
  const fields = Object.keys(frames[0][1]);
  for (let i = 1; i < frames.length; i++) {
    if (t <= frames[i][0]) {
      const f = between(t, frames[i - 1][0], frames[i][0]);
      return Object.fromEntries(fields.map(k => [k, mix(frames[i - 1][1][k], frames[i][1][k], f)]));
    }
  }
  return {...frames.at(-1)[1]};
}

// A two-link limb, clamped to its reachable annulus. Every pose keeps its bones.
export function joint(start, end, upper, lower, bend = 1) {
  const dx = end[0] - start[0], dy = end[1] - start[1];
  const raw = Math.hypot(dx, dy);
  const distance = clamp(raw, Math.abs(upper - lower) + .01, upper + lower - .01);
  const angle = raw > .0001 ? Math.atan2(dy, dx) : Math.PI / 2;
  const along = (upper * upper - lower * lower + distance * distance) / (2 * distance);
  const across = Math.sqrt(Math.max(0, upper * upper - along * along)) * bend;
  return {
    elbow: [start[0] + along * Math.cos(angle) - across * Math.sin(angle),
      start[1] + along * Math.sin(angle) + across * Math.cos(angle)],
    end: [start[0] + distance * Math.cos(angle), start[1] + distance * Math.sin(angle)],
  };
}

export function sampleStory(seconds) {
  const time = clamp(Number.isFinite(seconds) ? seconds : 0, 0, DURATION);
  const scene = Math.min(5, Math.floor(time / SCENE_SECONDS));
  const t = time - scene * SCENE_SECONDS;
  const p = {scene, t, x: 460, y: 440, lean: 0, head: 0, look: 0, gazeY: 0,
    left: [-65, -64], right: [65, -64], feet: [[-25, -8], [26, -8]],
    item: '', itemX: 0, itemY: -120, itemAngle: 0, itemOpacity: 1,
    smile: 1, talk: 0, blink: 1, page: 0, plane: 0, wave: 0, night: 0,
    tileX: 375, tileY: 320, subtitle: '', action: '', camera: 1};
  const wave = Math.sin(t * 4.5);
  if (scene === 0) {
    const walking = t > 1.2 && t < 3.6;
    p.x = mix(230, 520, between(t, 1.2, 3.6)) - 45 * between(t, 5.2, 7.2);
    p.y += walking ? -7 * Math.abs(Math.sin((t - 1.2) * 8)) : -1.5 * Math.sin(t * 2);
    p.head = -12 * between(t, .3, .8) + 22 * between(t, 3.7, 4.8);
    p.look = 10; p.gazeY = t > 4.5 ? 7 : 0;
    if (walking) {
      const stride = Math.sin((t - 1.2) * 8);
      p.feet = [[-25 + stride * 29, -8 - Math.max(0, stride) * 19],
        [26 - stride * 29, -8 - Math.max(0, -stride) * 19]];
      p.left = [-54 - stride * 20, -86]; p.right = [54 + stride * 20, -86];
      p.lean = 0;
    } else {
      const reach = between(t, 3.6, 4.15), lift = between(t, 4.2, 5.1);
      p.right = [mix(60, 117, reach) - 52 * lift, mix(-109, -111, reach) - 65 * lift];
      p.left = [-65, -64 - 20 * between(t, .5, 1) * (1 - between(t, 1.05, 1.2))];
    }
    p.item = 'phone';
    p.itemX = mix(650 - p.x, 80, between(t, 4.2, 5.1));
    p.itemY = mix(323 - p.y, -194, between(t, 4.2, 5.1));
    p.itemAngle = mix(6 * Math.sin(t * 22) * (1 - between(t, 1, 1.4)), -12, between(t, 4.2, 5.1));
    p.subtitle = t < 3.7 ? '叮咚——好像有人找我。' : '纸飞机？我记得我们聊过。';
    p.action = walking ? '走过去看看消息' : t < 4.2 ? '听到消息，转头' : '拿起手机，读一读';
  } else if (scene === 1) {
    p.x = 455; p.item = 'book'; p.itemX = 0; p.itemY = -113;
    p.left = [-61, -108]; p.right = [61, -108];
    p.head = mix(-3, 9, between(t, .4, 1.3)) - 12 * between(t, 5.2, 6.1);
    p.gazeY = 8 * (1 - between(t, 5, 6)); p.look = Math.sin(t * 2) * 5;
    p.page = between(t, 2, 3.2); p.itemAngle = 3 * Math.sin(t * 1.4);
    p.left[1] += Math.sin(t * 1.4) * 3; p.right[1] -= Math.sin(t * 1.4) * 3;
    p.right[0] -= 38 * Math.sin(p.page * Math.PI);
    p.right[1] -= 28 * Math.sin(p.page * Math.PI);
    p.subtitle = t < 4.5 ? '翻翻上次的记忆……找到了！' : '原来想试的，是另一种折法。';
    p.action = t < 2 ? '低头找相关记忆' : t < 3.3 ? '抬手翻过一页' : '读完，抬头想起来';
  } else if (scene === 2) {
    p.x = 445; p.item = 'mic'; p.itemX = 75; p.itemY = -152;
    p.right = [75, -145]; p.head = Math.sin(t * 1.8) * 6;
    p.left = t < 4 ? [-74, -137 + wave * 7] : [-100, -200 + Math.sin(t * 8) * 14];
    p.talk = t > .7 && t < 5.5 ? (.3 + .7 * Math.abs(Math.sin(t * 9))) : 0;
    p.lean = Math.sin(t * 1.5) * 3; p.look = -2;
    p.subtitle = t < 4 ? '记得呀，要不要试试另一种折法？' : '一句话、一张表情，或者一段语音。';
    p.action = t < 4 ? '拿着麦克风说话' : '转向你，轻轻挥手';
  } else if (scene === 3) {
    const sit = between(t, .2, 1.3);
    p.x = 330; p.y = mix(435, 440, sit); p.lean = 7;
    p.feet = [[mix(-25, -3, sit), -4], [mix(26, 66, sit), -8]];
    p.head = 9 + Math.sin(t * 1.7) * 4; p.look = 11; p.gazeY = -3;
    p.left = [-45, -97]; p.right = [62, -113];
    p.plane = between(t, 3.2, 6.7);
    if (t > 5) p.right = [88, -176 + wave * 7];
    p.subtitle = t < 4.4 ? '咦，这段讲了纸飞机的重心。' : '有意思，想和你分享这个片段。';
    p.action = t < 1.4 ? '坐下来，看看 B 站' : t < 5 ? '跟着画面看纸飞机' : '抬手，想到一个分享';
    p.camera = 1 + .035 * between(t, 1, 6);
  } else if (scene === 4) {
    const stretch = between(t, 4, 5.2), settle = between(t, 6.3, 7.8);
    p.item = 'game'; p.itemX = 0; p.itemY = -113 + Math.sin(t * 5) * 2;
    p.itemAngle = Math.sin(t * 5) * 4; p.itemOpacity = 1 - between(t, 3.7, 4.2);
    p.left = [mix(-56, -97, stretch), mix(-112, -233, stretch) + 100 * settle];
    p.right = [mix(56, 97, stretch), mix(-112, -233, stretch) + 100 * settle];
    p.head = Math.sin(t * 2) * 4 - 9 * stretch; p.look = -4;
    p.gazeY = 8 * (1 - stretch); p.talk = .65 * stretch * (1 - settle);
    p.night = between(t, 4.4, 7); p.blink = 1 - .7 * stretch;
    p.subtitle = t < 4 ? '再试一局 2048，就去写今天的日记。' : '伸个懒腰。今天也要记得休息呀。';
    p.action = t < 4 ? '双手握住手柄，玩一会儿' : '放下手柄，伸一个懒腰';
  } else {
    const k = keys(t, [[0, {x: 450, rx: 65, ry: -64, lx: -75, ly: -120}],
      [1.6, {x: 450, rx: 65, ry: -64, lx: -85, ly: -195}],
      [2.2, {x: 450, rx: 65, ry: -64, lx: -90, ly: -205}],
      [4.4, {x: 450, rx: 70, ry: -120, lx: -65, ly: -64}],
      [5.6, {x: 450, rx: 98, ry: -217, lx: -65, ly: -64}],
      [8, {x: 450, rx: 98, ry: -217, lx: -65, ly: -64}]]);
    p.x = k.x; p.right = [k.rx, k.ry + (t > 5 ? Math.sin(t * 8) * 12 : 0)];
    p.left = [k.lx, k.ly]; p.head = t < 4 ? -12 : 4 * Math.sin(t * 2);
    p.look = t < 4 ? -8 : 0; p.wave = between(t, .3, 3.3);
    const release = between(t, 2.2, 3.6);
    p.tileX = mix(p.x + p.left[0], 250, release);
    p.tileY = mix(p.y + p.left[1] - 16, 350, release) - 35 * Math.sin(release * Math.PI);
    p.subtitle = t < 4.7 ? '把喜欢的能力，一块一块接进来。' : '接下来，让她拥有自己的日常。';
    p.action = t < 3.5 ? '抬手，把新工具接上' : '转向你，挥手告别';
  }
  const blink = (time + .23) % 3.7;
  if (blink < .16) p.blink *= Math.abs(blink - .08) / .08;
  return p;
}
