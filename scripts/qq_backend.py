"""QQ connection backends, independent of conversation policy and the panel.

Only the selected provider supplies credentials. An invalid selection or an
unreadable account override never falls back to another running QQ backend.
"""
from dataclasses import dataclass, field
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request

def _conf():
    # Resolve the current deployment/configuration context, including isolated
    # source/install fixtures, without capturing another workspace's module.
    import project_conf
    return project_conf


class BackendError(RuntimeError):
    """A public, credential-free backend failure."""


class BackendConfigurationMissing(BackendError):
    """The vendor has not generated its first configuration yet."""


@dataclass(frozen=True)
class BackendSpec:
    key: str
    name: str
    directory: str
    config_name: str
    network_key: str
    token_key: str
    ws_clients: str
    ws_servers: str
    webui_port: int
    repository: str
    account_overlays: bool = False
    app_pattern: str = ''
    entry_points: tuple = ()


BACKENDS = {
    'napcat': BackendSpec('napcat', 'NapCatQQ', 'napcat/framework',
        'onebot11.json', 'network', 'token', 'websocketClients',
        'websocketServers', 6099, 'https://github.com/NapNeko/NapCatQQ', app_pattern=r'NapCat(?:\.Onebot|QQ)?',
        entry_points=('napimain.exe', 'napiloader.dll', 'nativeLoader.cjs')),
    'snowluma': BackendSpec('snowluma', 'SnowLuma', 'snowluma',
        'onebot.json', 'networks', 'accessToken', 'wsClients', 'wsServers',
        5099, 'https://github.com/SnowLuma/SnowLuma', True, r'SnowLuma', ('index.mjs', 'node.exe' if os.name == 'nt' else 'node')),
}


def _object(value, reason='Invalid backend configuration'):
    if not isinstance(value, dict):
        raise BackendError(reason)
    return value


def _read(path, *, optional=False):
    """Bounded ordinary JSON; explicit corrupt files are authoritative failures."""
    try:
        for part in [*reversed(path.parents), path]:
            if os.path.lexists(part):
                info = part.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise BackendError('Backend configuration links are not supported')
        before = path.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > 4 * 1024 * 1024:
            raise BackendError('Invalid backend configuration file')
        raw = path.read_bytes()
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise BackendError('Backend configuration changed while reading')
        value = _object(json.loads(raw.decode('utf-8-sig')))
        return value, hashlib.sha256(raw).hexdigest()
    except FileNotFoundError:
        if optional and not os.path.lexists(path):
            return None, None
        raise BackendConfigurationMissing('Backend configuration is missing') from None
    except (OSError, ValueError, UnicodeError):
        raise BackendError('Backend configuration is unreadable or invalid') from None


def _port(value):
    if type(value) is not int or not 1 <= value <= 65535:
        raise BackendError('Invalid backend port')
    return value


def local_url(value, schemes=('http', 'https')):
    """Endpoints are local configuration, never URLs or hosts from messages."""
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        raise BackendError('Invalid backend URL')
    try:
        parsed = urllib.parse.urlsplit(value)
        if (parsed.scheme not in schemes or parsed.hostname not in ('127.0.0.1', 'localhost', '::1') or
                parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment):
            raise ValueError()
        if parsed.port is not None:
            _port(parsed.port)
        if '\\' in parsed.path or any(part in ('.', '..') for part in parsed.path.split('/')) or '%' in parsed.path:
            raise ValueError()
        return value.rstrip('/')
    except (ValueError, BackendError):
        raise BackendError('Backend URL must use an explicit local endpoint') from None


def _rows(network, key):
    rows = network.get(key, [])
    if not isinstance(rows, list) or len(rows) > 64 or any(not isinstance(row, dict) for row in rows):
        raise BackendError('Invalid backend network list')
    for row in rows:
        if 'enabled' in row and type(row['enabled']) is not bool:
            raise BackendError('Invalid backend network enabled flag')
    return rows


def _network(document, spec):
    # The current SnowLuma schema is networks; the legacy top-level shape is
    # read without rewriting it, so upgrades remain the provider's responsibility.
    if spec.network_key in document:
        return _object(document[spec.network_key])
    if spec.account_overlays:
        return document
    return {}


def _overlay(global_document, account, spec):
    """Named adapters overlay by name; a snapshot excludes global adapters."""
    sources = ([global_document] if global_document is not None else [])
    if account is not None:
        if account.get('mode') == 'snapshot':
            sources = []
        sources.append(account)
    result = {}
    for kind in ('httpServers', 'httpClients', spec.ws_clients, spec.ws_servers):
        named = {}; unnamed = 0
        for source in sources:
            network = _object(source.get(spec.network_key, {}))
            rows = _rows(network, kind)
            if kind == 'httpClients':
                rows += _rows(source, 'httpPostEndpoints')
            rows += _rows(source, kind)
            for row in rows:
                name = row.get('name')
                if name in (None, ''):
                    prefix = {'httpServers': 'http', 'httpClients': 'httppost',
                        spec.ws_clients: 'wsclient', spec.ws_servers: 'ws'}[kind]
                    unnamed += 1; name = prefix + '-' + str(unnamed)
                    while name in named:
                        unnamed += 1; name = prefix + '-' + str(unnamed)
                if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', name):
                    raise BackendError('Invalid backend adapter name')
                # Replacement is of the complete adapter, not field merging:
                # an override cannot inherit a global credential by accident.
                named[name] = dict(row, name=name)
        result[kind] = list(named.values())
    return result


@dataclass(frozen=True)
class Connection:
    provider: str
    name: str
    base_url: str
    token: str = field(repr=False)
    fingerprint: str
    config_files: tuple
    webui_url: str
    network: dict = field(repr=False, compare=False)
    spec: BackendSpec = field(repr=False, compare=False)

    def public(self):
        def endpoints(kind):
            out = []
            for row in _rows(self.network, kind):
                if row.get('enabled', True) is False:
                    continue
                item = {key: row[key] for key in ('name', 'host', 'port', 'path', 'role') if key in row}
                # Reverse URLs can contain access_token. Never return those
                # credentials, even in the local panel or exception messages.
                if isinstance(row.get('url'), str):
                    try:
                        url = urllib.parse.urlsplit(row['url'])
                        if url.username is None and url.password is None:
                            item['url'] = urllib.parse.urlunsplit((url.scheme, url.netloc, url.path, '', ''))
                    except ValueError:
                        item['url'] = '(invalid endpoint)'
                item['tokenConfigured'] = bool(row.get(self.spec.token_key))
                out.append(item)
            return out
        return dict(provider=self.provider, name=self.name, available=True,
            base_url=self.base_url, webui_url=self.webui_url,
            token_configured=bool(self.token), config_fingerprint=self.fingerprint,
            config_files=[str(path) for path in self.config_files],
            http=endpoints('httpServers'), wsClient=endpoints(self.spec.ws_clients),
            wsServer=endpoints(self.spec.ws_servers))


def describe(project=None, workspace=None, provider=None, registry=None):
    """Resolve provider metadata without requiring that it is installed yet."""
    project = _object(project if project is not None else _conf().load(reload=True))
    workspace = Path(workspace if workspace is not None else _conf().work_dir()).absolute()
    options = _object(project.get('qqBackend', {}))
    key = provider if provider is not None else options.get('provider', 'napcat')
    registry = BACKENDS if registry is None else registry
    if not isinstance(key, str) or key not in registry:
        raise BackendError('Unknown QQ backend; no fallback was selected')
    spec = registry[key]
    settings = _object(_object(options.get('providers', {})).get(key, {}))
    root = settings.get('root', spec.directory)
    if not isinstance(root, str) or not root or '\x00' in root:
        raise BackendError('Invalid backend installation directory')
    root = Path(root).expanduser()
    root = root.absolute() if root.is_absolute() else (workspace / root).absolute()
    config = root / 'config' / spec.config_name
    if 'configFile' in settings:
        value = settings['configFile']
        if not isinstance(value, str) or not value or '\x00' in value:
            raise BackendError('Invalid backend configuration path')
        config = Path(value) if Path(value).is_absolute() else root / value
    return spec, settings, root, config, project, workspace


def resolve(project=None, workspace=None, provider=None, expected_account=None, registry=None):
    spec, settings, root, config, project, workspace = describe(project, workspace, provider, registry)
    files, pins = [], []
    global_document, pin = _read(config, optional=spec.account_overlays)
    if global_document is not None:
        files.append(config); pins.append(pin)
    if spec.account_overlays:
        account = settings.get('account', expected_account)
        if account is None:
            name = project.get('dataDirName', _conf().DEFAULTS['dataDirName'])
            if not isinstance(name, str) or not name or name in ('.', '..') or '/' in name or '\\' in name:
                raise BackendError('Invalid configured identity directory')
            identity, _ = _read(workspace / name / 'lanya-config.json', optional=True)
            account = (identity or {}).get('botQQ')
        account_document = None
        if account not in (None, ''):
            if not isinstance(account, (str, int)) or isinstance(account, bool) or not re.fullmatch(r'[1-9][0-9]{0,19}', str(account)):
                raise BackendError('Invalid configured QQ account')
            account_path = root / 'config' / ('onebot_' + str(account) + '.json')
            account_document, pin = _read(account_path, optional=True)
            if account_document is not None:
                files.append(account_path); pins.append(pin)
        if not files:
            raise BackendConfigurationMissing('Selected backend has no OneBot configuration')
        network = _overlay(global_document, account_document, spec)
    else:
        network = _network(global_document, spec)
    servers = [row for row in _rows(network, 'httpServers') if row.get('enabled', True) is not False]
    name = settings.get('httpServer')
    if 'httpServer' in settings:
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', name):
            raise BackendError('Invalid selected HTTP adapter name')
        servers = [row for row in servers if row.get('name') == name]
    elif spec.key == 'napcat':
        port = _port(project.get('onebotPort', 8720))
        servers = [row for row in servers if row.get('port') == port]
    if len(servers) != 1:
        raise BackendError('Select one enabled HTTP adapter explicitly')
    server = servers[0]; port = _port(server.get('port', 3000 if spec.account_overlays else None))
    host = server.get('host', '127.0.0.1')
    if host in ('0.0.0.0', '::', ''):
        host = '127.0.0.1'
    if host == '::1':
        host = '[::1]'
    prefix = server.get('path', '/')
    if not isinstance(prefix, str) or not prefix.startswith('/') or re.search(r'\s', prefix):
        raise BackendError('Invalid backend HTTP path')
    base = local_url('http://' + str(host) + ':' + str(port) + prefix)
    token = server.get(spec.token_key, '')
    if not isinstance(token, str) or len(token) > 4096 or any(ord(c) < 32 for c in token):
        raise BackendError('Invalid backend access token')
    webui_url = webui_address(spec, settings, root)
    options = project.get('qqBackend', {})
    revision = options.get('revision', 0)
    if type(revision) is not int or not 0 <= revision <= 9007199254740991:
        raise BackendError('Invalid backend selection revision')
    fingerprint = hashlib.sha256(json.dumps([spec.key, revision, settings, base, pins], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return Connection(spec.key, spec.name, base, token, fingerprint, tuple(files), webui_url, network, spec)


def installed(spec, root):
    return bool(spec.entry_points) and all((root / name).is_file() and
        not (root / name).is_symlink() and not getattr((root / name).lstat(), 'st_file_attributes', 0) & 0x400
        for name in spec.entry_points)


def webui_address(spec, settings, root):
    port = settings.get('webuiPort', spec.webui_port)
    if spec.account_overlays:
        runtime, _ = _read(root / 'config/runtime.json', optional=True)
        if runtime is not None and 'webuiPort' not in settings:
            port = runtime.get('webuiPort', spec.webui_port)
    return local_url('http://127.0.0.1:' + str(_port(port)))


def catalog(project=None, workspace=None):
    """Local installation/configuration facts, without starting or contacting QQ."""
    project = project if project is not None else _conf().load(reload=True)
    selected, _, _, _, _, _ = describe(project, workspace)
    providers = []
    for key in BACKENDS:
        spec, settings, root, _, _, _ = describe(project, workspace, provider=key)
        row = dict(provider=key, name=spec.name, selected=key == selected.key,
            installed=installed(spec, root), repository=spec.repository, configured=False, can_select=False)
        try:
            row['webui_url'] = webui_address(spec, settings, root)
            row['connection'] = resolve(project, workspace, provider=key).public()
            row['configured'] = True; row['can_select'] = True
        except BackendError as error:
            row['error'] = str(error)
            row['can_select'] = row['installed'] and isinstance(error, BackendConfigurationMissing)
        providers.append(row)
    return dict(selected=selected.key, providers=providers,
        protocol='onebot-v11', runtime_verified=False)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BackendError('Backend redirect refused')


def call(action, payload=None, *, connection=None, timeout=10, max_bytes=4 * 1024 * 1024):
    if not isinstance(action, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,79}', action):
        raise BackendError('Invalid OneBot action')
    connection = connection if connection is not None else resolve()
    body = json.dumps({} if payload is None else payload, ensure_ascii=False).encode()
    request = urllib.request.Request(connection.base_url + '/' + action, data=body,
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + connection.token}, method='POST')
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise BackendError('OneBot HTTP request failed')
            raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise BackendError('OneBot response exceeds limit')
        result = _object(json.loads(raw.decode('utf-8')))
        if result.get('status') != 'ok' or type(result.get('retcode')) is not int or result['retcode'] != 0:
            raise BackendError('OneBot action rejected')
        return result
    except (OSError, ValueError, UnicodeError, urllib.error.URLError):
        raise BackendError('OneBot request failed; delivery is not confirmed') from None


def health(connection=None, expected_account=None, *, timeout=3):
    connection = connection if connection is not None else resolve()
    status = call('get_status', connection=connection, timeout=timeout)['data']
    info = call('get_login_info', connection=connection, timeout=timeout)['data']
    version = call('get_version_info', connection=connection, timeout=timeout)['data']
    _object(status); _object(info); _object(version)
    account = str(info.get('user_id', ''))
    logged_in = bool(re.fullmatch(r'[1-9][0-9]{0,19}', account))
    app = version.get('app_name', '')
    provider_matches = bool(isinstance(app, str) and connection.spec.app_pattern and re.fullmatch(connection.spec.app_pattern, app, re.I))
    account_matches = expected_account in (None, '') or str(expected_account) == account
    return dict(provider=connection.provider, online=status.get('online') is True and provider_matches and account_matches,
        good=status.get('good') is True, logged_in=logged_in,
        provider_matches=provider_matches, account_matches=account_matches,
        # Callers needing the exact account compare in memory, never publish it.
        account=account, source='onebot_read_only')


def runtime_status():
    """Sanitized read-only status for component tools, never a send."""
    conf = _conf(); project = conf.load(reload=True)
    spec, _, _, _, _, _ = describe(project=project)
    out = dict(provider=spec.key, name=spec.name, verified=False, status='unconfirmed',
        source='onebot_read_only', client_delivery_verified=False, observed_at=int(time.time() * 1000))
    try:
        connection = resolve(project=project)
        expected, _ = _read(Path(conf.data_dir()) / 'lanya-config.json', optional=True)
        value = health(connection, expected_account=(expected or {}).get('botQQ'))
        value.pop('account', None)
        verified = all(value.get(key) is True for key in ('online', 'good', 'logged_in', 'provider_matches', 'account_matches'))
        out.update(value, verified=verified, status='online' if verified else 'login_or_connection_required',
            config_fingerprint=connection.fingerprint, connection=connection.public())
    except BackendError:
        out['status'] = 'configuration_or_connection_unavailable'
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', choices=sorted(BACKENDS))
    parser.add_argument('--connection-private', action='store_true', help='Private pipe only; includes credential, never log')
    parser.add_argument('--status', action='store_true', help='Read-only status without credentials or account identifiers')
    args = parser.parse_args()
    try:
        if args.status:
            print(json.dumps(runtime_status(), ensure_ascii=True))
            return 0
        connection = resolve(provider=args.provider)
        if args.connection_private:
            value = dict(provider=connection.provider, napcatHttp=connection.base_url, napcatToken=connection.token,
                webui_url=connection.webui_url)
        else:
            value = connection.public()
        print(json.dumps(value, ensure_ascii=True))
        return 0
    except BackendError as error:
        print(json.dumps(dict(ok=False, error=str(error))))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
