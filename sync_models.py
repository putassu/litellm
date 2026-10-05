#!/usr/bin/env python3
"""Создать/обновить deployments из models.json через API. Модели не удаляются.

В контейнере, после готовности шлюза (переменные передаёт Compose):
  docker compose exec -T litellm python /opt/setup/sync_models.py
Только одна модель:
  docker compose exec -T litellm python /opt/setup/sync_models.py --model local-bge-m3
Предварительный просмотр без сети:
  python3 sync_models.py --dry-run
"""
import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request

# Пулы хранятся в models.json рядом с этим скриптом. При изменении цепочки
# измените также
# router_settings.fallbacks в litellm_config.yaml.
# RPD ниже — справочная квота; переход происходит после ответа Google 429,
# а не по собственному счётчику 500 запросов.
MODELS_FILE = Path(__file__).resolve().with_name("models.json")


def desired_models(manifest, local_ip):
    deployments = []
    for model in manifest['cloud_models']:
        for project in range(1, 4):
            deployments.append({
                'model_name': model['name'],
                'litellm_params': {
                    'model': model['provider_model'],
                    'api_key': f'os.environ/GEMINI_KEY_PROJ_{project}',
                    'order': project,
                    'rpm': model['router_rpm'],
                    'tpm': model['router_tpm'],
                    'max_parallel_requests': 1,
                    'timeout': 180,
                    'stream_timeout': 120,
                },
                'model_info': {
                    'id': f"{model['name']}-proj{project}",
                    'mode': 'chat',
                    'cooldown_time': 86400,
                    'description': (
                        f"Проект {project}; приоритет {project}; "
                        f"Google: {model['google_rpm']} RPM, {model['google_tpm']} TPM, "
                        f"{model['google_rpd']} RPD. RPD здесь — справочная квота."
                    ),
                    'managed_by': 'litellm-proxy-bundle',
                },
            })
    for model in manifest['local_models']:
        # Infinity may expose /embeddings at the root; other servers use /v1.
        api_path = model.get('api_base_path', '/v1').strip('/')
        api_base = f"http://{local_ip}:{model['port']}"
        if api_path:
            api_base += '/' + api_path
        params = {
            'model': model['provider_model'],
            'api_base': api_base,
            'api_key': 'sk-dummy-key',
            'timeout': model['timeout'],
        }
        info = {
            'id': model['name'], 'mode': model['mode'],
            'description': model['description'],
            'input_cost_per_token': 0, 'output_cost_per_token': 0,
            'cooldown_time': 30, 'managed_by': 'litellm-proxy-bundle',
        }
        if model.get('max_input_tokens'):
            info['max_input_tokens'] = model['max_input_tokens']
        if model.get('max_output_tokens'):
            info['max_output_tokens'] = model['max_output_tokens']
            params['max_tokens'] = model['max_output_tokens']
        deployments.append({'model_name': model['name'], 'litellm_params': params, 'model_info': info})
    return deployments


def redact(text):
    for name, value in os.environ.items():
        if value and any(word in name for word in ('KEY', 'PASSWORD', 'TOKEN', 'DATABASE_URL')):
            text = text.replace(value, '[REDACTED]')
    return text[:1500]


class API:
    def __init__(self, base, key):
        self.base = base.rstrip('/')
        self.key = key
        # Administrative calls are local; never send a master key to the outbound proxy.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={'Authorization': f'Bearer {self.key}', 'Content-Type': 'application/json'},
        )
        try:
            with self.opener.open(request, timeout=60) as response:
                body = response.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            detail = redact(exc.read().decode(errors='replace'))
            raise RuntimeError(f'{method} {path}: HTTP {exc.code}: {detail}') from None


def synchronize(api, desired):
    response = api.call('GET', '/model/info')
    if not isinstance(response.get('data'), list):
        raise RuntimeError('/model/info returned no data list; nothing changed')
    existing = {m.get('model_info', {}).get('id'): m for m in response['data']}
    # Preflight collisions before the first mutation.
    for model in desired:
        identifier = model['model_info']['id']
        if identifier in existing and existing[identifier]['model_name'] != model['model_name']:
            raise RuntimeError(f'ID collision: {identifier}; nothing changed')
    for model in desired:
        identifier = model['model_info']['id']
        if identifier in existing:
            path = '/model/' + urllib.parse.quote(identifier, safe='') + '/update'
            api.call('PATCH', path, model)
            print(f'UPDATED {identifier}')
        else:
            api.call('POST', '/model/new', model)
            print(f'CREATED {identifier}')
    print(f'Synchronized {len(desired)} deployments. Unrelated models were preserved.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='Print the manifest without contacting any service.')
    parser.add_argument('--model', help='Sync only this model_name, e.g. local-bge-m3.')
    args = parser.parse_args()
    manifest = json.loads(MODELS_FILE.read_text(encoding='utf-8'))
    desired = desired_models(manifest, os.environ.get('LOCAL_AI_IP', '127.0.0.1'))
    if args.model:
        desired = [model for model in desired if model['model_name'] == args.model]
        if not desired:
            raise RuntimeError(f'Unknown model_name: {args.model}; nothing changed')
    if args.dry_run:
        print(json.dumps(desired, indent=2, ensure_ascii=False))
        return
    if not os.environ.get('LITELLM_MASTER_KEY'):
        raise RuntimeError('Missing LITELLM_MASTER_KEY; run via docker compose exec; nothing changed')
    # GEMINI_KEY_PROJ_* are resolved by the gateway, which must have them in its
    # container environment. The host script sends only os.environ/... references.
    synchronize(API('http://127.0.0.1:4000', os.environ['LITELLM_MASTER_KEY']), desired)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError) as exc:
        print('ERROR: ' + redact(str(exc)), file=sys.stderr)
        raise SystemExit(1)
