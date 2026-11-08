  tool({
    name: 'bili_frames',
    description: '看视频里的画面（从 B 站自带的故事板裁 1-6 帧，每帧 480×270）。\n'
      + '想知道"这条视频到底拍了什么"就用它 —— 字幕告诉你说什么，画面告诉你拍的是什么。\n'
      + '默认按时长均匀取 3 帧；想盯某个时刻（比如字幕里提到的那段）就传 at。',
    parameters: {
      bvid: { type: 'string', description: 'BV号' },
      num: { type: 'number', description: '取几帧，1-6，默认 3' },
      at: { type: 'array', items: { type: 'number' }, description: '指定时间点（秒）列表；传了就以它为准，忽略 num' },
    },
    output: imageOutput,
    async execute(args, exec) {
      const at = Array.isArray(args.at) ? args.at.map(Number).filter(Number.isFinite) : undefined
      const { json, images } = await callRaw('bili_frames', {
        bvid: String(args.bvid ?? ''),
        num: Math.max(1, Math.min(Number(args.num) || 3, 6)),
        ...(at && at.length ? { at } : {}),
      }, { signal: exec?.signal })
      const meta = json && typeof json === 'object' ? json : { raw: json }
      if (!meta.ok) return meta
      if (!images.length) return { ...meta, ok: false, error: 'MCP 没返回图片' }

      const a = await attachImages(exec, images)
      return a.ok
        ? { ...meta, image_attached: true, images: a.refs }
        : { ...meta, image_attached: false, image_skip_reason: a.reason, hint: a.hint }
    },
  })
