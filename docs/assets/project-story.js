import {DURATION, SCENE_SECONDS, sampleStory, joint} from './project-story-motion.js';

// A silent, local animated film. One clock owns character, props and subtitles.
const player = document.querySelector('[data-project-story]');
if (player) {
  const scenes = [
    ['CONNECT', '叮咚，一条消息来了。', 'NapCatQQ / SnowLuma 负责 QQ 连接，主 LLM 结合当前群聊、私聊与引用，判断怎样回应。'],
    ['REMEMBER', '翻翻记忆，原来是这件事。', '短期线索、长期事实、时间和来源一起提供上下文。记忆可以更正，日记保留有依据的经历。'],
    ['EXPRESS', '把想说的话，自然说出来。', '选择文字、真实表情或 GPT-SoVITS 本地语音。轻松的时候，也可以只发一张表情。'],
    ['EXPLORE', '刷到有趣的，想和你分享。', '读取有时间范围的 B 站字幕与画面，再决定是否评论、回复或 @ 管理员；覆盖与结果分别记录。'],
    ['LIVE', '玩一会儿，也记得休息。', '自主考虑浏览、游戏、记日记或安静休息。预算、休息时间与联系间隔继续生效。'],
    ['EXTEND', '给她的世界，再添一点可能。', '组合 QQ、B 站、语音和记忆，沿 DSH 插件与本机执行器接入自己的工具。新能力需适配与验证。'],
  ];
  const $ = selector => player.querySelector(selector);
  const rig = Object.fromEntries([...player.querySelectorAll('[data-rig]')].map(n => [n.dataset.rig, n]));
  const toggle = $('[data-story-toggle]'), seek = $('[data-story-seek]');
  const previous = $('[data-story-previous]'), next = $('[data-story-next]');
  const chapters = [...player.querySelectorAll('[data-story-step]')];
  const status = $('[data-story-status]'), clock = $('[data-story-clock]');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  let elapsed = 0, frame = null, lastStamp = null, chapter = -1, lastClock = '';
  let visible = !('IntersectionObserver' in window), pageActive = true;
  let wanted = !reduced.matches && !document.body.classList.contains('capture');
  const fmt = n => Number(n.toFixed(3));
  const move = (name, x, y, r = 0, scale = 1) => rig[name]?.setAttribute('transform',
    `translate(${fmt(x)} ${fmt(y)}) rotate(${fmt(r)}) scale(${fmt(scale)})`);
  const alpha = (name, value) => { if (rig[name]) rig[name].style.opacity = String(fmt(value)); };
  const path = (name, value) => rig[name].setAttribute('d', value);
  const point = v => v.map(fmt).join(' ');
  function limb(name, start, end, upper, lower, bend) {
    const k = joint(start, end, upper, lower, bend);
    path(name + '-upper', `M${point(start)} L${point(k.elbow)}`);
    path(name + '-lower', `M${point(k.elbow)} L${point(k.end)}`);
    if (name.endsWith('arm')) {
      const angle = Math.atan2(k.end[1] - k.elbow[1], k.end[0] - k.elbow[0]) * 180 / Math.PI;
      move(name + '-hand', ...k.end, angle - 90);
    } else move(name + '-shoe', ...k.end);
  }
  function draw() {
    const p = sampleStory(elapsed);
    player.dataset.step = String(p.scene);
    move('actor', p.x, p.y, p.lean);
    move('shadow', p.x, 448, 0, p.scene === 3 ? .85 : 1);
    move('head', 0, -191, p.head);
    move('back-hair', 0, -191, p.head);
    move('eyes', p.look, p.gazeY);
    rig.lids.setAttribute('transform', `scale(1 ${fmt(Math.max(.06, p.blink))})`);
    path('mouth', `M-10 25 Q0 ${fmt(34 + p.talk * 9)} 10 25 Q0 ${fmt(34 - p.talk * 15)} -10 25`);
    limb('left-leg', [-21, -77], p.feet[0], 37, 36, -1);
    limb('right-leg', [21, -77], p.feet[1], 37, 36, -1);
    limb('left-arm', [-38, -132], p.left, 44, 45, -1);
    limb('right-arm', [38, -132], p.right, 44, 45, 1);
    for (const n of ['phone', 'book', 'mic', 'game']) {
      rig[n].style.display = p.item === n ? '' : 'none';
      if (p.item === n) { move(n, p.itemX, p.itemY, p.itemAngle); alpha(n, p.itemOpacity); }
    }
    rig.page.setAttribute('transform', `scale(${fmt(Math.cos(p.page * Math.PI))} 1)`);
    move('paper-plane', 714 - 270 * p.plane, 230 - 135 * Math.sin(p.plane * Math.PI * .8), -12 - p.plane * 18, 1 + p.plane * .55);
    move('tile-moving', p.tileX, p.tileY, 15 * (1 - p.wave));
    alpha('night', p.night * .35);
    alpha('notification', p.scene === 0 ? Math.max(0, 1 - Math.max(0, p.t - 3) / .8) : 0);
    move('notification', 0, Math.sin(p.t * 10) * (p.t < 1 ? 3 : 0));
    move('camera', 480 * (1 - p.camera), 280 * (1 - p.camera), 0, p.camera);
    const subtitle = $('[data-story-subtitle]');
    if (subtitle.textContent !== p.subtitle) subtitle.textContent = p.subtitle;
    $('[data-story-action]').textContent = p.action;
    if (p.scene !== chapter) {
      chapter = p.scene;
      $('[data-story-eyebrow]').textContent = `0${chapter + 1} / ${scenes[chapter][0]}`;
      $('[data-story-title]').textContent = scenes[chapter][1];
      $('[data-story-description]').textContent = scenes[chapter][2];
      for (const n of player.querySelectorAll('[data-story-node]')) n.style.display = Number(n.dataset.storyNode) === chapter ? '' : 'none';
      for (const b of chapters) {
        if (Number(b.dataset.storyStep) === chapter) b.setAttribute('aria-current', 'step');
        else b.removeAttribute('aria-current');
      }
      previous.disabled = chapter === 0; next.disabled = chapter === 5;
    }
    seek.value = String(elapsed);
    seek.style.setProperty('--story-progress', `${elapsed / DURATION * 100}%`);
    const stamp = '00:' + String(Math.floor(elapsed)).padStart(2, '0') + ' / 00:48';
    if (stamp !== lastClock) { clock.textContent = stamp; lastClock = stamp; }
    seek.setAttribute('aria-valuetext', `${Math.floor(elapsed)} 秒，共 48 秒；${scenes[chapter][1]}`);
  }
  function controls() {
    const ended = elapsed >= DURATION;
    player.dataset.playing = String(wanted && visible && pageActive && !document.hidden && !ended);
    toggle.textContent = ended ? '↻ 重播' : wanted ? 'Ⅱ 暂停' : '▶ 播放';
    toggle.setAttribute('aria-label', ended ? '重播项目介绍动画' : wanted ? '暂停项目介绍动画' : '播放项目介绍动画');
  }
  function tick(stamp) {
    frame = null;
    if (!wanted || !visible || !pageActive || document.hidden) { lastStamp = null; controls(); return; }
    if (lastStamp !== null) elapsed = Math.min(DURATION, elapsed + Math.max(0, (stamp - lastStamp) / 1000));
    lastStamp = stamp; draw();
    if (elapsed >= DURATION) {
      wanted = false; lastStamp = null; controls();
      status.textContent = '短片播放完毕，可以重播或拖动进度查看。'; return;
    }
    frame = requestAnimationFrame(tick);
  }
  function reconcile() {
    if (wanted && visible && pageActive && !document.hidden && elapsed < DURATION) {
      if (frame === null) { lastStamp = null; frame = requestAnimationFrame(tick); }
    } else {
      if (frame !== null) cancelAnimationFrame(frame);
      frame = null; lastStamp = null;
    }
    controls();
  }
  function jump(time) {
    wanted = false; elapsed = Math.min(DURATION, Math.max(0, time));
    draw(); reconcile(); status.textContent = `已暂停在 ${elapsed.toFixed(1)} 秒。`;
  }
  for (const b of chapters) {
    b.disabled = false;
    b.addEventListener('click', () => jump(Number(b.dataset.storyStep) * SCENE_SECONDS));
  }
  for (const b of [toggle, previous, next, $('[data-story-restart]')]) b.disabled = false;
  seek.disabled = false;
  seek.addEventListener('input', () => jump(Number(seek.value)));
  previous.addEventListener('click', () => jump(Math.max(0, chapter - 1) * SCENE_SECONDS));
  next.addEventListener('click', () => jump(Math.min(5, chapter + 1) * SCENE_SECONDS));
  toggle.addEventListener('click', () => {
    if (elapsed >= DURATION) elapsed = 0;
    wanted = !wanted; draw(); reconcile();
    if (wanted) $('.story-screen').scrollIntoView({block: 'center', behavior: reduced.matches ? 'instant' : 'smooth'});
    status.textContent = wanted ? '正在播放无声动画短片。' : '已暂停。';
  });
  $('[data-story-restart]').addEventListener('click', () => {
    elapsed = 0; wanted = true; draw(); reconcile(); status.textContent = '从头播放无声动画短片。';
    $('.story-screen').scrollIntoView({block: 'center', behavior: reduced.matches ? 'instant' : 'smooth'});
  });
  document.addEventListener('visibilitychange', reconcile);
  reduced.addEventListener('change', () => { if (reduced.matches) { wanted = false; reconcile(); } });
  if ('IntersectionObserver' in window) {
    const observer = new IntersectionObserver(entries => {
      visible = entries[0].isIntersecting; reconcile();
    }, {threshold: 0});
    observer.observe($('.story-screen'));
  }
  window.addEventListener('pagehide', () => { pageActive = false; reconcile(); });
  window.addEventListener('pageshow', () => { pageActive = true; reconcile(); });
  draw(); reconcile();
}
