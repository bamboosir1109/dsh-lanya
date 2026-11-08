// A local, illustrative project story. No network, model, media or platform calls.
(() => {
  'use strict';
  const player = document.querySelector('[data-project-story]');
  if (!player) return;
  const scenes = [
    {
      eyebrow: '01 / CONNECT', title: '叮咚，一条消息来了。',
      description: '选择 NapCatQQ 或 SnowLuma，让群聊、私聊与引用来到同一套角色系统。QQ 后端负责连接，主 LLM 负责决定。',
      label: '来信示例', example: '“还记得我们聊的纸飞机吗？”',
      state: '接收当前上下文', nodes: ['qq'],
    },
    {
      eyebrow: '02 / REMEMBER', title: '翻翻记忆，原来是这件事。',
      description: '把眼前的话题与相关记忆放在一起。短期线索、长期事实、时间和来源，让回应有上下文，也允许更正。',
      label: '检索示例', example: '纸飞机 · 共同话题 · 来源：先前对话',
      state: '检索相关记忆', nodes: ['memory'],
    },
    {
      eyebrow: '03 / EXPRESS', title: '一句话，一张图，或一段语音。',
      description: '主 LLM 选择文字、真实表情或本地语音。GPT-SoVITS 合成后再核对发送，轻松的时候也可以只发一张图。',
      label: '回应示例 · 波形无声音', example: '“记得呀，要不要试试另一种折法？”',
      state: '选择表达方式', nodes: ['voice'],
    },
    {
      eyebrow: '04 / EXPLORE', title: '刷到有趣的，想和你分享。',
      description: '在 B 站读取有时间范围的字幕和画面，再判断是否评论、回复或 @ 管理员。内容覆盖和互动结果分别记录。',
      label: '活动示例', example: '纸飞机的重心 · 00:18–00:43 内容片段',
      state: '选择内容与互动', nodes: ['bili'],
    },
    {
      eyebrow: '05 / LIVE', title: '玩一会儿，也记得休息。',
      description: '浏览、玩一小会儿、整理日记，或安静休息。自主机会让她重新考虑下一步，休息、预算和联系间隔继续生效。',
      label: '计划示例', example: '试试 2048，再把有依据的进展记下来。',
      state: '安排下一件事', nodes: ['activity'],
    },
    {
      eyebrow: '06 / EXTEND', title: '给她的世界，再添一点可能。',
      description: '把 QQ、B 站、语音和记忆按需组合，再沿 DSH 插件与本机执行器接入自己的工具。新能力仍需适配和验证。',
      label: '你的集成底座', example: '同一主 LLM · 可信上下文 · 可扩展的工具与服务',
      state: '统一调用工具', nodes: ['extend'],
    },
  ];
  const perScene = 6000;
  const duration = scenes.length * perScene;
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const chapters = [...player.querySelectorAll('[data-story-step]')];
  const toggle = player.querySelector('[data-story-toggle]');
  const previous = player.querySelector('[data-story-previous]');
  const next = player.querySelector('[data-story-next]');
  const restart = player.querySelector('[data-story-restart]');
  const progress = player.querySelector('[data-story-progress]');
  const clock = player.querySelector('[data-story-clock]');
  const status = player.querySelector('[data-story-status]');
  let elapsed = 0;
  let index = -1;
  let frame = null;
  let lastStamp = null;
  let inView = !('IntersectionObserver' in window);
  let wanted = !reduced.matches && !document.body.classList.contains('capture');
  let entered = false;
  let copyAnimation = null;
  let lastSecond = -1;

  function render() {
    const selected = Math.min(scenes.length - 1, Math.floor(elapsed / perScene));
    if (selected !== index) {
      const animateCopy = index !== -1 && !reduced.matches && inView && !document.hidden;
      index = selected;
      const scene = scenes[index];
      player.dataset.step = String(index);
      for (const [key, value] of Object.entries({eyebrow: scene.eyebrow, title: scene.title,
        description: scene.description, label: scene.label, example: scene.example, state: scene.state})) {
        player.querySelector('[data-story-' + key + ']').textContent = value;
      }
      for (const chapter of chapters) {
        if (Number(chapter.dataset.storyStep) === index) chapter.setAttribute('aria-current', 'step');
        else chapter.removeAttribute('aria-current');
      }
      for (const node of player.querySelectorAll('[data-story-node]'))
        node.dataset.on = String(scene.nodes.includes(node.dataset.storyNode));
      previous.disabled = index === 0;
      next.disabled = index === scenes.length - 1;
      copyAnimation?.cancel();
      if (animateCopy && player.querySelector('.story-copy').animate) {
        copyAnimation = player.querySelector('.story-copy').animate(
          [{opacity: .4, transform: 'translateY(6px)'}, {opacity: 1, transform: 'translateY(0)'}],
          {duration: 360, easing: 'cubic-bezier(.2,.7,.3,1)'});
      }
    }
    progress.style.setProperty('--story-progress', String(elapsed / duration));
    const second = Math.floor(elapsed / 1000);
    if (second !== lastSecond) {
      lastSecond = second;
      clock.textContent = '00:' + String(second).padStart(2, '0') + ' / 00:36';
      progress.parentElement.setAttribute('aria-valuenow', String(Math.round(elapsed / duration * 100)));
    }
  }

  function controls() {
    const finished = elapsed >= duration;
    toggle.textContent = finished ? '重播' : wanted ? '暂停' : '播放';
    toggle.setAttribute('aria-label', finished ? '重播项目介绍动画' : wanted ? '暂停项目介绍动画' : '播放项目介绍动画');
    player.dataset.playing = String(wanted && inView && !document.hidden && !finished);
  }

  function tick(stamp) {
    frame = null;
    if (!wanted || !inView || document.hidden) { lastStamp = null; controls(); return; }
    if (lastStamp !== null) elapsed = Math.min(duration, elapsed + Math.max(0, Math.min(stamp - lastStamp, 250)));
    lastStamp = stamp;
    render();
    if (elapsed >= duration) {
      wanted = false;
      lastStamp = null;
      status.textContent = '介绍播放完成，可以重播或选择任一章节。';
      controls();
      return;
    }
    frame = requestAnimationFrame(tick);
  }

  function reconcile() {
    const run = wanted && inView && !document.hidden && elapsed < duration;
    if (!run) {
      if (frame !== null) cancelAnimationFrame(frame);
      frame = null;
      lastStamp = null;
      copyAnimation?.cancel();
    } else if (frame === null) {
      lastStamp = null;
      frame = requestAnimationFrame(tick);
    }
    controls();
  }

  function seek(selected) {
    wanted = false;
    elapsed = selected * perScene;
    render();
    reconcile();
    status.textContent = '已选择第 ' + (selected + 1) + ' 章：' + scenes[selected].title;
  }
  for (const chapter of chapters) {
    chapter.disabled = false;
    chapter.addEventListener('click', () => seek(Number(chapter.dataset.storyStep)));
  }
  for (const button of [toggle, previous, next, restart]) button.disabled = false;
  previous.addEventListener('click', () => seek(Math.max(0, index - 1)));
  next.addEventListener('click', () => seek(Math.min(scenes.length - 1, index + 1)));
  restart.addEventListener('click', () => {
    elapsed = 0; wanted = true; render(); reconcile();
    status.textContent = '从第一章重新播放。';
  });
  toggle.addEventListener('click', () => {
    if (elapsed >= duration) elapsed = 0;
    wanted = !wanted;
    render(); reconcile();
    status.textContent = wanted ? '继续播放。' : '已暂停，可以逐章查看。';
  });
  document.addEventListener('visibilitychange', reconcile);
  reduced.addEventListener('change', () => {
    if (reduced.matches) { wanted = false; copyAnimation?.cancel(); reconcile(); }
  });
  let observer = null;
  if ('IntersectionObserver' in window) {
    observer = new IntersectionObserver(entries => {
      inView = entries[0].isIntersecting && entries[0].intersectionRatio >= .12;
      // Do not start while merely loading a hash link to another section.
      if (inView) entered = true;
      reconcile();
    }, {threshold: .12});
    observer.observe(player);
  }
  window.addEventListener('pagehide', () => {
    if (frame !== null) cancelAnimationFrame(frame);
    frame = null; lastStamp = null; copyAnimation?.cancel();
    player.dataset.playing = 'false';
  });
  window.addEventListener('pageshow', () => {
    if (entered || !observer) reconcile();
  });
  render();
  reconcile();
})();
