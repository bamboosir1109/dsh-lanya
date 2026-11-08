// Exact generic media exports from dsh-onebot-qq 0.8.1; see provenance.json.
import { realpath, stat, readFile } from 'node:fs/promises';
import { sep } from 'node:path';
export async function resolveContainedPath(allowedRoots, target) {
    let resolved;
    try {
        resolved = await realpath(target);
    }
    catch {
        return null;
    }
    for (const root of allowedRoots) {
        let rootReal;
        try {
            rootReal = await realpath(root);
        }
        catch {
            continue; // a missing root cannot contain anything
        }
        const prefix = rootReal.endsWith(sep) ? rootReal : rootReal + sep;
        if (resolved === rootReal || resolved.startsWith(prefix))
            return resolved;
    }
    return null;
}
/**
 * Read a local file as a base64 data URI for OneBot media segments.
 * @param path - absolute file path.
 * @param maxBytes - size cap; exceeding it throws.
 * @param allowedRoots - when provided, the path is refused unless it
 *   resolves inside one of the roots (M1-A3a outbound fence; tools pass it
 *   once wired, until then absence keeps the legacy uncaged behavior).
 * @returns "base64://<data>".
 */
export async function fileToBase64(path, maxBytes, allowedRoots) {
    if (allowedRoots !== undefined && (await resolveContainedPath(allowedRoots, path)) === null) {
        throw new Error('path not allowed or does not exist: ' + path);
    }
    const info = await stat(path);
    if (info.size > maxBytes) {
        throw new Error('file too large: ' + info.size + ' bytes (limit ' + maxBytes + ')');
    }
    const data = await readFile(path);
    return 'base64://' + data.toString('base64');
}
/** Whether a string looks like a remote URL. */
export function isUrl(value) {
    return /^https?:\/\//.test(value);
}
