/**
 * OneBot 11 message parsing: CQ-code unescaping, segment-array → text/media
 * extraction, mention detection, and face-id → emoji mapping. Ported from the
 * Hermes onebot_utils.py (CQ parsing half). Pure functions, no I/O.
 * @module dsh-onebot/cq
 */
/**
 * Reverse the CQ escaping NapCat applies inside attribute values
 * (&amp; → &, &#91; → [, &#93; → ], &#44; → ,). Skipping this broke
 * image-CDN downloads with a 403 (the & in signed URLs arrived escaped).
 * @param value - raw CQ attribute value or text.
 * @returns unescaped value.
 */
export function cqUnescape(value) {
    return value
        .replaceAll('&#91;', '[')
        .replaceAll('&#93;', ']')
        .replaceAll('&#44;', ',')
        .replaceAll('&amp;', '&');
}
/** QQ face id → emoji (common set; unknown ids fall back to a neutral face). */
const FACE_EMOJI = {
    '0': '😀', '1': '😁', '2': '😂', '3': '🤣', '4': '😃', '5': '😄', '6': '😅', '7': '😆',
    '8': '😉', '9': '😊', '10': '😋', '11': '😎', '12': '😍', '13': '😘', '14': '🥰', '15': '😗',
    '18': '😙', '19': '😚', '20': '😐', '21': '😑', '22': '😶', '23': '😏', '24': '😣', '25': '😥',
    '26': '😮', '27': '🤐', '28': '😯', '29': '😪', '30': '😫', '31': '🥱', '32': '😴', '33': '😌',
    '34': '😛', '35': '😜', '36': '😝', '37': '🤤', '38': '😒', '39': '😓', '40': '😔', '41': '😕',
    '42': '🙃', '43': '🤑', '44': '😲', '45': '☹️', '46': '🙁', '47': '😖', '48': '😞', '49': '😟',
    '50': '😤', '51': '😢', '52': '😭', '53': '😦', '54': '😧', '55': '😨', '56': '😩', '57': '🤯',
    '58': '😬', '59': '😰', '60': '😱', '61': '🥵', '62': '🥶', '63': '😳', '64': '🤪', '65': '😵',
    '66': '😡', '67': '😠', '68': '🤬', '69': '😷', '70': '🤒', '71': '🤕', '72': '🤢', '73': '🤮',
    '74': '🥳', '75': '🥴', '76': '😺', '77': '😸', '78': '😹', '79': '😻', '80': '😼', '81': '😽',
    '82': '🙀', '83': '😿', '84': '😾', '96': '🙈', '97': '🙉', '98': '🙊', '99': '💋',
    '100': '💘', '101': '💝', '102': '💖', '103': '💗', '104': '💓', '105': '💞', '106': '💕',
    '107': '💌', '108': '💔', '109': '💟', '110': '❤️', '111': '🧡', '112': '💛', '113': '💚',
    '114': '💙', '115': '💜', '116': '🤎', '117': '🖤', '118': '🤍', '121': '👍', '122': '👎',
    '123': '👌', '124': '👏', '125': '🙏', '126': '🤝', '127': '💪', '128': '🤙', '129': '✌️',
    '130': '🤞', '131': '🖕', '132': '🤟', '133': '🤘', '134': '🤛', '135': '🤜', '136': '👈',
    '137': '👉', '138': '👆', '139': '👇', '140': '☝️', '141': '✋', '142': '🤚', '143': '🖐️',
    '144': '🖖', '145': '👋', '146': '🤗', '147': '🤔', '148': '🤫', '149': '🤭', '150': '🤥',
    '151': '🤨', '152': '🧐', '153': '🤓', '154': '😈', '155': '👿', '156': '👹', '157': '👺',
    '158': '💀', '159': '👻', '160': '👽', '161': '🤖', '170': '👶', '171': '👧',
    '172': '🧒', '173': '👦', '174': '👩', '175': '🧑', '176': '👨', '177': '👵', '178': '🧓',
    '179': '👴', '180': '👲', '181': '👳', '182': '🧕', '183': '👮', '184': '👷', '185': '💂',
    '186': '🕵️', '199': '👩‍🏭', '200': '👨‍🏭', '201': '👩‍💻', '202': '👨‍💻', '203': '👩‍💼',
    '204': '👨‍💼', '205': '👩‍🔧', '206': '👨‍🔧', '207': '👩‍🔬', '208': '👨‍🔬', '209': '👩‍🎨',
    '210': '👨‍🎨', '211': '👩‍🚒', '212': '👨‍🚒', '213': '👩‍✈️', '214': '👨‍✈️', '215': '👩‍🚀',
    '216': '👨‍🚀', '217': '👩‍⚖️', '218': '👨‍⚖️', '219': '🦸', '225': '🦹', '231': '🧙',
    '237': '🧚', '243': '🧛', '249': '🧜', '255': '🧞', '261': '🧟', '267': '🦄',
    '268': '🦍', '269': '🐶', '270': '🐱', '271': '🦁', '272': '🐯', '273': '🐨',
    '274': '🐼', '275': '🐻', '276': '🦊', '277': '🐸', '278': '🐵', '279': '🐔',
    '280': '🐧', '281': '🐦', '282': '🐤', '283': '🦆', '284': '🦅', '285': '🦉',
    '286': '🐴', '287': '🦄', '288': '🐝', '289': '🐛', '290': '🦋', '291': '🐌',
    '292': '🐞', '293': '🐜', '294': '🦟', '295': '🦗', '296': '🕷️', '297': '🦂',
    '298': '🐢', '299': '🐍', '300': '🦎', '301': '🦖', '302': '🦕', '303': '🐙',
    '304': '🦑', '305': '🦐', '306': '🦞', '307': '🦀', '308': '🐡', '309': '🐠',
    '310': '🐟', '311': '🐬', '312': '🐳', '313': '🐋', '314': '🦈', '315': '🐊',
    '316': '🐅', '317': '🐆', '318': '🦓', '319': '🦌', '320': '🦙', '321': '🐪',
    '322': '🐫', '323': '🦒', '324': '🐘', '325': '🦏', '326': '🦛', '327': '🐭',
    '328': '🐹', '329': '🐰', '330': '🐿️', '331': '🦔', '332': '🦇', '333': '🐻',
    '334': '🐺', '335': '🐗', '336': '🐮', '337': '🐷', '338': '🐽', '339': '🐏',
    '340': '🐑', '341': '🐐', '342': '🐪', '343': '🐂', '344': '🐃', '345': '🐄',
    '346': '🐎', '347': '🐖', '348': '🐀', '349': '🐁', '350': '🐈', '351': '🐩',
    '352': '🐕', '353': '🐓', '354': '🦃', '355': '🦚', '356': '🦜', '357': '🦢',
    '358': '🦩', '359': '🕊️', '360': '🐇', '361': '🦝', '362': '🦨', '363': '🦡',
    '364': '🦦', '365': '🦥', '366': '🐁', '367': '🐉', '368': '🐲', '369': '🐢',
};
/** Map a QQ face id to an emoji, with a fallback for unknown ids. */
export function faceToEmoji(id) {
    return FACE_EMOJI[id] ?? '😀';
}
const CQ_AT_RE = /\[CQ:at,qq=([^,\]]+)\]/g;
const CQ_REPLY_RE = /\[CQ:reply,id=([^,\]]+)\]/g;
/**
 * Detect whether the message mentions the bot. Prefers the segment array;
 * falls back to CQ-string scanning. @-detection stays fail-closed: with an
 * unknown bot id and no configured botQQ, group messages are never treated
 * as mentioning us. Reply segments count only when the replied-to user is
 * determinably the bot itself (M1-A8); an undeterminable target (segment
 * carries no qq) falls back to the previous always-mention behavior — a
 * deliberate fail-open so tightening the gate never breaks existing setups.
 * @param segments - segment array (may be undefined for CQ-only payloads).
 * @param raw - raw CQ string.
 * @param selfId - learned bot QQ id ('' when unknown).
 * @param botQQ - configured bot QQ id ('' when unset).
 */
export function detectMention(segments, raw, selfId, botQQ) {
    const bot = selfId !== '' ? selfId : botQQ;
    if (segments !== undefined) {
        for (const seg of segments) {
            if (seg.type === 'reply') {
                const qq = seg.data.qq ?? '';
                if (qq === '' || qq === bot)
                    return true;
                continue;
            }
            if (bot !== '' && seg.type === 'at' && (seg.data.qq === bot || seg.data.qq === 'all'))
                return true;
        }
        return false;
    }
    if (bot === '')
        return false;
    CQ_AT_RE.lastIndex = 0;
    for (const m of raw.matchAll(CQ_AT_RE)) {
        if (m[1] === bot || m[1] === 'all')
            return true;
    }
    CQ_REPLY_RE.lastIndex = 0;
    return CQ_REPLY_RE.test(raw);
}
/** Split a raw CQ string into segments; used only when no segment array exists. */
export function parseCqString(raw) {
    const segments = [];
    const re = /\[CQ:([a-z]+),([^\]]*)\]/g;
    let last = 0;
    for (const m of raw.matchAll(re)) {
        if (m.index !== undefined && m.index > last) {
            segments.push({ type: 'text', data: { text: raw.slice(last, m.index) } });
        }
        const data = {};
        for (const pair of m[2].split(',')) {
            const eq = pair.indexOf('=');
            if (eq > 0)
                data[pair.slice(0, eq)] = cqUnescape(pair.slice(eq + 1));
        }
        segments.push({ type: m[1], data });
        last = (m.index ?? 0) + m[0].length;
    }
    if (last < raw.length) {
        segments.push({ type: 'text', data: { text: raw.slice(last) } });
    }
    return segments;
}
/**
 * Parse a OneBot message (segment array preferred, CQ string fallback) into
 * text + media references. Media appear as placeholders in the text; media.ts
 * resolves each ref to a local path afterwards.
 * @param segments - message segment array (preferred).
 * @param raw - raw CQ string (fallback / complement).
 * @returns parsed text, media refs, and the quoted message id.
 */
export function parseMessage(segments, raw) {
    const textParts = [];
    const media = [];
    let replyId;
    let forwardId;
    const appendImage = (data) => {
        const subType = data.sub_type ?? data['sub-type'];
        if (subType === '1') {
            textParts.push('[表情包]');
            return;
        }
        media.push({ kind: 'image', url: data.url || undefined, file: data.file || undefined, subType });
        textParts.push('[图片]');
    };
    const list = segments !== undefined && segments.length > 0 ? segments : parseCqString(raw);
    for (const seg of list) {
        switch (seg.type) {
            case 'text':
                if (seg.data.text !== '')
                    textParts.push(seg.data.text);
                break;
            case 'image':
                appendImage(seg.data);
                break;
            case 'face':
                textParts.push(faceToEmoji(seg.data.id ?? ''));
                break;
            case 'record':
                media.push({ kind: 'voice', url: seg.data.url || undefined, file: seg.data.file || undefined });
                textParts.push('[语音]');
                break;
            case 'video':
                media.push({ kind: 'video', url: seg.data.url || undefined, file: seg.data.file || undefined });
                textParts.push('[视频]');
                break;
            case 'file':
                // NapCat's file segment: `file` holds the file NAME (not a path),
                // `file_id` the real id, `name` is usually absent. Keep the raw
                // fields for media.ts; the display name falls back to `file`.
                media.push({
                    kind: 'file',
                    url: seg.data.url || undefined,
                    file: seg.data.file || undefined,
                    fileId: seg.data.file_id || undefined,
                    name: seg.data.name || seg.data.file || undefined,
                });
                textParts.push(seg.data.name || seg.data.file ? `[文件:${seg.data.name || seg.data.file}]` : '[文件]');
                break;
            case 'at': {
                const qq = seg.data.qq ?? '';
                textParts.push(qq === 'all' ? '@全体成员' : `@${qq}`);
                break;
            }
            case 'reply':
                if (seg.data.id !== undefined) {
                    replyId = seg.data.id;
                    textParts.push('[引用]');
                }
                break;
            case 'forward':
                // NapCat uses `id`; go-cqhttp-style implementations may use `resId`.
                if (seg.data.id !== undefined || seg.data.resId !== undefined) {
                    forwardId = seg.data.id ?? seg.data.resId;
                    textParts.push('[合并转发]');
                }
                break;
            case 'json':
                textParts.push('[卡片]');
                break;
            case 'poke':
                textParts.push('[戳一戳]');
                break;
            case 'contact':
                textParts.push(seg.data.name ? `[联系人:${seg.data.name}]` : '[联系人]');
                break;
            case 'location':
                textParts.push('[位置]');
                break;
            case 'share':
                textParts.push(seg.data.title ? `[分享:${seg.data.title}]` : '[分享]');
                break;
            default:
                // Unknown segment types keep their placeholder role; no crash.
                textParts.push(`[${seg.type}]`);
                break;
        }
    }
    const text = textParts.join('').trim();
    return { text, media, replyId, forwardId };
}
/** Extract the plain text of a quoted (get_msg) message for [引用] expansion. */
export function segmentText(segments, raw) {
    return parseMessage(segments, raw).text;
}
/** Marker regex shared with split.ts (outbound [[qq_forward]]). */
export const FORWARD_BLOCK_RE = /\[\[qq_forward\]\]([\s\S]*?)\[\[\/qq_forward\]\]/g;
