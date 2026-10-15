import hashlib
import json
import queue
import sys
import threading
import tkinter as tk
import uuid
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from urllib.parse import urlparse
try:
    import websocket
except ImportError:
    websocket = None
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
TOKEN_FILE = APP_DIR / 'vts_token.json'
PLUGIN_NAME = 'VTS Natural Expressor'
PLUGIN_DEVELOPER = 'ForLive'
PROFILE_FILE = APP_DIR / 'profiles.json'
LEGACY_RULES_FILE = APP_DIR / 'rules.json'

def default_vts_url() -> str:
    if sys.platform.startswith('linux'):
        try:
            release = Path('/proc/sys/kernel/osrelease').read_text(encoding='utf-8').lower()
            if 'microsoft' in release or 'wsl' in release:
                routes = Path('/proc/net/route').read_text(encoding='ascii').splitlines()[1:]
                for route in routes:
                    fields = route.split()
                    if len(fields) > 2 and fields[1] == '00000000':
                        gateway = bytes.fromhex(fields[2])
                        host = '.'.join((str(part) for part in gateway[::-1]))
                        return f'ws://{host}:8001'
        except (OSError, ValueError):
            pass
    return 'ws://127.0.0.1:8001'

class VTSClient:
    def __init__(self, url: str, events: queue.Queue):
        self.url = url
        self.events = events
        self.stop_event = threading.Event()
        self.ws = None
        self.lock = threading.Lock()
        self.rules_lock = threading.Lock()
        self.rules = []
        self.rule_states = {}
        self.pending_deactivations = queue.Queue()

    def set_rules(self, rules: list[dict]):
        updated = [dict(rule) for rule in rules]
        with self.rules_lock:
            old_by_id = {rule.get('id'): rule for rule in self.rules}
            new_by_id = {rule.get('id'): rule for rule in updated}
            for rule_id, old_rule in old_by_id.items():
                new_rule = new_by_id.get(rule_id)
                if self.rule_states.get(rule_id) and (new_rule is None or not new_rule.get('enabled', True) or old_rule.get('expression_file') != new_rule.get('expression_file')):
                    self.pending_deactivations.put(old_rule.get('expression_file'))
                    self.rule_states[rule_id] = False
            self.rules = updated

    def set_model_rules(self, rules: list[dict]):
        with self.rules_lock:
            self.rules = [dict(rule) for rule in rules]
            self.rule_states.clear()
            while True:
                try:
                    self.pending_deactivations.get_nowait()
                except queue.Empty:
                    break

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self.stop_event.set()
        with self.lock:
            if self.ws:
                try:
                    self.ws.close()
                except Exception:
                    pass

    def _request(self, ws, message_type: str, data: dict | None=None):
        payload = {'apiName': 'VTubeStudioPublicAPI', 'apiVersion': '1.0', 'requestID': str(uuid.uuid4()), 'messageType': message_type, 'data': data or {}}
        ws.send(json.dumps(payload))
        while not self.stop_event.is_set():
            reply = json.loads(ws.recv())
            if reply.get('requestID') == payload['requestID']:
                if reply.get('messageType') == 'APIError':
                    raise RuntimeError(reply.get('data', {}).get('message', 'VTS API error'))
                return reply
        raise RuntimeError('切断しました')

    def _run(self):
        ws = None
        try:
            if websocket is None:
                raise RuntimeError('websocket-client がありません。requirements.txt の依存をインストールしてください。')
            ws = websocket.create_connection(self.url, timeout=6)
            with self.lock:
                self.ws = ws
            token = None
            if TOKEN_FILE.exists():
                try:
                    token_data = json.loads(TOKEN_FILE.read_text(encoding='utf-8'))
                    if token_data.get('pluginName') == PLUGIN_NAME:
                        token = token_data.get('token')
                except (OSError, json.JSONDecodeError):
                    token = None

            if token:
                auth = self._request(ws, 'AuthenticationRequest', {'pluginName': PLUGIN_NAME, 'pluginDeveloper': PLUGIN_DEVELOPER, 'authenticationToken': token})
                if not auth.get('data', {}).get('authenticated'):
                    token = None
            if not token:
                result = self._request(ws, 'AuthenticationTokenRequest', {'pluginName': PLUGIN_NAME, 'pluginDeveloper': PLUGIN_DEVELOPER})
                token = result.get('data', {}).get('authenticationToken')
                if not token:
                    raise RuntimeError('VTSでプラグインのAPIアクセスを許可してください。')
                TOKEN_FILE.write_text(json.dumps({'pluginName': PLUGIN_NAME, 'token': token}, ensure_ascii=False, indent=2), encoding='utf-8')
                auth = self._request(ws, 'AuthenticationRequest', {'pluginName': PLUGIN_NAME, 'pluginDeveloper': PLUGIN_DEVELOPER, 'authenticationToken': token})
                if not auth.get('data', {}).get('authenticated'):
                    raise RuntimeError('VTS APIの認証に失敗しました。')
            self.events.put(('status', 'VTSに接続しました'))
            expressions = self._request(ws, 'ExpressionStateRequest', {'details': False})
            self.events.put(('expressions', expressions.get('data', {}).get('expressions', [])))
            last_model_id = None

            while not self.stop_event.is_set():
                while True:
                    try:
                        expression_file = self.pending_deactivations.get_nowait()
                    except queue.Empty:
                        break
                    if expression_file:
                        try:
                            self._request(ws, 'ExpressionActivationRequest', {'expressionFile': expression_file, 'active': False})
                        except Exception as exc:
                            self.events.put(('error', f'表情解除エラー: {exc}'))

                live = self._request(ws, 'Live2DParameterListRequest')
                live_data = live.get('data', {})
                self.events.put(('live', live_data))
                model_id = live_data.get('modelID')

                if model_id != last_model_id:
                    last_model_id = model_id
                    if model_id:
                        expressions = self._request(ws, 'ExpressionStateRequest', {'details': False})
                        self.events.put(('expressions', expressions.get('data', {}).get('expressions', [])))

                inputs = self._request(ws, 'InputParameterListRequest')
                input_data = inputs.get('data', {})
                self.events.put(('inputs', input_data))
                self._evaluate_rules(ws, input_data)

                found = self._request(ws, 'FaceFoundRequest')
                self.events.put(('face', found.get('data', {}).get('found')))
                self.stop_event.wait(0.2)
        except Exception as exc:
            if not self.stop_event.is_set():
                self.events.put(('error', f'接続エラー: {exc}'))
        finally:
            with self.lock:
                if self.ws:
                    try:
                        self.ws.close()
                    except Exception:
                        pass
                    self.ws = None
            self.events.put(('disconnected', None))

    def _evaluate_rules(self, ws, data: dict):
        available = data.get('defaultParameters', []) + data.get('customParameters', [])
        values = {p.get('name'): p.get('value') for p in available if p.get('name') if isinstance(p.get('value'), (int, float))}
        with self.rules_lock:
            rules = [dict(rule) for rule in self.rules]
            rule_states = dict(self.rule_states)
        for rule in rules:
            rule_id = rule.get('id')
            if not rule_id:
                continue
            if not rule.get('enabled'):
                continue
            conditions = rule.get('conditions', [])
            current = [(condition, values.get(condition.get('source'))) for condition in conditions]
            if current:
                if any((value is None for _, value in current)):
                    continue
            was_active = rule_states.get(rule_id, False)
            checks_on = [value <= float(condition['on_threshold']) if condition.get('direction') == 'low' else value >= float(condition['on_threshold']) for condition, value in current]
            checks_off = [value >= float(condition['off_threshold']) if condition.get('direction') == 'low' else value <= float(condition['off_threshold']) for condition, value in current]
            if rule.get('mode', 'all') == 'any':
                should_activate = any(checks_on)
                should_deactivate = all(checks_off)
            else:
                should_activate = all(checks_on)
                should_deactivate = any(checks_off)
            if not was_active and should_activate:
                new_state = True
            elif was_active and should_deactivate:
                new_state = False
            else:
                continue
            try:
                self._request(ws, 'ExpressionActivationRequest', {'expressionFile': rule['expression_file'], 'active': new_state})
                with self.rules_lock:
                    self.rule_states[rule_id] = new_state
                self.events.put(('rule_state', (rule_id, new_state)))
            except Exception as exc:
                self.events.put(('error', f'表情切替エラー: {exc}'))
            continue


class FeatureMonitor:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title('VTS Natural Expressor')
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()
        width = min(1280, max(800, screen_width - 80))
        height = min(1000, max(600, screen_height - 80))
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 2)
        root.geometry(f'{width}x{height}+{x}+{y}')
        root.minsize(min(900, width), min(650, height))
        self.events = queue.Queue()
        self.client = None
        self.connection_failed = False
        self.url = tk.StringVar(value=default_vts_url())
        self.status = tk.StringVar(value='未接続')
        self.face_status = tk.StringVar(value='未取得')
        self.model_status = tk.StringVar(value='モデル情報待ち')
        self.profile_name = tk.StringVar()
        self.profile_choice = tk.StringVar()
        self.profile_model = tk.StringVar(value='モデル未接続')
        self.input_updated = tk.StringVar(value='VTS入力: --')
        self.live_updated = tk.StringVar(value='Live2D値: --')
        self.filter_text = tk.StringVar()
        self.rows = {}
        self.rules = []
        self.profiles = {}
        self.active_profile_id = None
        self.active_model_id = None
        self.profiles_digest = None
        self.profile_save_conflict = False
        self.pending_legacy_rules = None
        self.input_parameter_names = []
        self.expressions = []
        self.rule_source = tk.StringVar()
        self.rule_direction = tk.StringVar(value='high')
        self.rule_expression = tk.StringVar()
        self.rule_on = tk.StringVar(value='0.7')
        self.rule_off = tk.StringVar(value='0.3')
        self.rule_mode = tk.StringVar(value='all')
        self.builder_conditions = []
        self.editing_rule_id = None
        self.editing_condition_index = None
        self._load_profiles()
        self._ui()
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.after(100, self._drain_events)

    @staticmethod
    def _normalize_rules(rules):
        normalized = [rule for rule in rules if isinstance(rule, dict) if rule.get('id')]
        for rule in normalized:
            if 'conditions' not in rule and rule.get('source'):
                rule['conditions'] = [{'source': rule['source'], 'direction': 'high', 'on_threshold': rule.get('on_threshold', 0.7), 'off_threshold': rule.get('off_threshold', 0.3)}]
                rule['mode'] = 'all'
        return normalized

    def _load_profiles(self):
        try:
            stored = json.loads(PROFILE_FILE.read_text(encoding='utf-8'))
            profiles = stored.get('profiles', {}) if isinstance(stored, dict) else {}
            self.profiles = profiles if isinstance(profiles, dict) else {}
            saved_profile_id = stored.get('active_profile_id') if isinstance(stored, dict) else None
            if saved_profile_id in self.profiles:
                self.active_profile_id = saved_profile_id
            saved_url = stored.get('vts_url') if isinstance(stored, dict) else None
            if saved_url:
                self.url.set(saved_url)
        except (OSError, json.JSONDecodeError):
            self.profiles = {}
            self.active_profile_id = None
        if self.profiles:
            migrated = {}
            for key, profile in self.profiles.items():
                if not isinstance(profile, dict):
                    continue
                profile_id = key if 'rules' in profile else str(uuid.uuid4())
                migrated[profile_id] = {'name': profile.get('name') or profile.get('model_name') or '移行したプロファイル', 'rules': self._normalize_rules(profile.get('rules', []))}
            self.profiles = migrated
            if self.active_profile_id not in self.profiles:
                self.active_profile_id = next(iter(self.profiles), None)
        if not self.profiles:
            try:
                old_rules = json.loads(LEGACY_RULES_FILE.read_text(encoding='utf-8'))
                if isinstance(old_rules, list):
                    self.pending_legacy_rules = self._normalize_rules(old_rules)
            except (OSError, json.JSONDecodeError):
                pass
        if not self.profiles and self.pending_legacy_rules is not None:
            profile_id = str(uuid.uuid4())
            self.profiles[profile_id] = {'name': '標準', 'rules': self.pending_legacy_rules}
            self.active_profile_id = profile_id
            self.pending_legacy_rules = None
        if self.active_profile_id in self.profiles:
            active_profile = self.profiles[self.active_profile_id]
            self.rules = [dict(rule) for rule in active_profile.get('rules', [])]
            self.profile_name.set(active_profile.get('name', ''))
            self.profile_choice.set(active_profile.get('name', ''))
        self.profiles_digest = self._get_profiles_digest()

    @staticmethod
    def _get_profiles_digest() -> str | None:
        try:
            contents = PROFILE_FILE.read_bytes()
        except FileNotFoundError:
            return
        except OSError:
            return
        return hashlib.sha256(contents).hexdigest()

    def _save_profiles(self):
        if self.profile_save_conflict:
            return
        if self._get_profiles_digest() != self.profiles_digest:
            self.profile_save_conflict = True
            messagebox.showerror('設定ファイルの競合', '起動後にprofiles.jsonが別の場所から変更されています。\n現在の内容で上書きしないよう保存を止めました。\nアプリを再起動して設定を読み直してください。')
            return
        try:
            PROFILE_FILE.write_text(json.dumps({'version': 1, 'vts_url': self.url.get().strip(), 'profiles': self.profiles, 'active_profile_id': self.active_profile_id}, ensure_ascii=False, indent=2), encoding='utf-8')
            self.profiles_digest = self._get_profiles_digest()
        except OSError as exc:
            messagebox.showerror('プロファイル保存エラー', str(exc))
            return

    def _switch_model_profile(self, model_id: str, model_name: str):
        if model_id == self.active_model_id:
            return
        if self.editing_rule_id:
            self._finish_edit()
        if self.active_profile_id and self.active_profile_id in self.profiles:
            self.profiles[self.active_profile_id]['rules'] = [dict(rule) for rule in self.rules]
        profile_id = self.active_profile_id
        profile = self.profiles.get(profile_id) if profile_id else None
        created_profile = profile is None
        if profile is None:
            initial_rules = self.pending_legacy_rules if self.pending_legacy_rules is not None else []
            profile_id = str(uuid.uuid4())
            profile = {'name': '標準', 'rules': initial_rules}
            self.pending_legacy_rules = None
            self.profiles[profile_id] = profile
            self.active_profile_id = profile_id
        profile.setdefault('name', '標準')
        profile['rules'] = self._normalize_rules(profile.get('rules', []))
        self.active_model_id = model_id
        self.rules = [dict(rule) for rule in profile['rules']]
        self.profile_name.set(profile['name'])
        self._refresh_profile_choices()
        self.profile_choice.set(profile['name'])
        self.profile_model.set(f'適用モデル: {model_name or model_id}')
        if self.client:
            self.client.set_model_rules(self.rules)
        self._render_rules()
        if created_profile:
            self._save_profiles()

    def _save_current_profile(self):
        if not self.active_profile_id or self.active_profile_id not in self.profiles:
            messagebox.showinfo('プロファイル保存', 'VTSに接続してモデルを読み込んでください。')
            return
        name = self.profile_name.get().strip()
        if not name:
            messagebox.showerror('プロファイル名', 'プロファイル名を入力してください。')
            return
        profile = self.profiles[self.active_profile_id]
        if any((p.get('name') == name for pid, p in self.profiles.items() if pid != self.active_profile_id)):
            messagebox.showerror('プロファイル名', '同じ名前のプロファイルが既にあります。')
            return
        profile['name'] = name
        profile['rules'] = [dict(rule) for rule in self.rules]
        self.profile_choice.set(name)
        self._refresh_profile_choices()
        self._save_profiles()

    def _refresh_profile_choices(self):
        if hasattr(self, 'profile_combo'):
            names = [profile.get('name', '無名プロファイル') for profile in self.profiles.values()]
            self.profile_combo.configure(values=names)

    def _select_profile(self, _event=None):
        name = self.profile_choice.get()
        profile_id = next((pid for pid, profile in self.profiles.items() if profile.get('name') == name), None)
        if not profile_id or profile_id == self.active_profile_id:
            return
        self.profiles[self.active_profile_id]['rules'] = [dict(rule) for rule in self.rules]
        self.active_profile_id = profile_id
        profile = self.profiles[profile_id]
        self.profile_name.set(profile['name'])
        self.rules = [dict(rule) for rule in self._normalize_rules(profile.get('rules', []))]
        if self.editing_rule_id:
            self._finish_edit()
        if self.client:
            self.client.set_rules(self.rules)
        self._render_rules()
        self._save_profiles()

    def _create_profile(self, duplicate=False):
        if self.active_profile_id:
            self.profiles[self.active_profile_id]['rules'] = [dict(rule) for rule in self.rules]
        base_name = self.profile_name.get().strip() or 'プロファイル'
        name = f'{base_name} のコピー' if duplicate else '新しいプロファイル'
        original = name
        suffix = 2
        existing_names = {p.get('name') for p in self.profiles.values()}
        while name in existing_names:
            name = f'{original} {suffix}'
            suffix += 1
        profile_id = str(uuid.uuid4())
        source_rules = self.rules if duplicate else []
        self.profiles[profile_id] = {'name': name, 'rules': [dict(rule) for rule in source_rules]}
        self.active_profile_id = profile_id
        self.profile_name.set(name)
        self.profile_choice.set(name)
        self.rules = [dict(rule) for rule in source_rules]
        if self.client:
            self.client.set_rules(self.rules)
        self._refresh_profile_choices()
        self._render_rules()
        self._save_profiles()

    def _delete_profile(self):
        if not self.active_profile_id:
            return
        if len(self.profiles) <= 1:
            messagebox.showinfo('プロファイル削除', '最後のプロファイルは削除できません。')
            return
        profile_name = self.profiles[self.active_profile_id].get('name', '')
        if not messagebox.askyesno('プロファイル削除', f'「{profile_name}」を削除しますか？'):
            return
        deleted_id = self.active_profile_id
        del self.profiles[deleted_id]
        replacement_id = next(iter(self.profiles))
        self.active_profile_id = replacement_id
        replacement = self.profiles[replacement_id]
        self.profile_name.set(replacement.get('name', ''))
        self.profile_choice.set(replacement.get('name', ''))
        self.rules = [dict(rule) for rule in replacement.get('rules', [])]
        if self.client:
            self.client.set_rules(self.rules)
        self._refresh_profile_choices()
        self._render_rules()
        self._save_profiles()

    def _sync_rules(self):
        if self.client:
            self.client.set_rules(self.rules)
        if self.active_profile_id and self.active_profile_id in self.profiles:
            self.profiles[self.active_profile_id]['rules'] = [dict(rule) for rule in self.rules]
            self._save_profiles()
        self._render_rules()

    @staticmethod
    def _expression_label(expression: dict) -> str:
        return f'{expression.get("name", expression.get("file", ""))} [{expression.get("file", "")}]'

    def _update_rule_choices(self, inputs: dict | None=None):
        if inputs is not None:
            params = inputs.get('defaultParameters', []) + inputs.get('customParameters', [])
            self.input_parameter_names = sorted({p.get('name') for p in params if p.get('name')})
        self.source_combo.configure(values=self.input_parameter_names)
        if self.rule_source.get() not in self.input_parameter_names and self.input_parameter_names:
            self.rule_source.set(self.input_parameter_names[0])
        labels = [self._expression_label(expression) for expression in self.expressions]
        self.expression_combo.configure(values=labels)
        if self.rule_expression.get() not in labels and labels:
            self.rule_expression.set(labels[0])

    def _render_rules(self):
        if not hasattr(self, 'rule_table'):
            return
        for iid in self.rule_table.get_children():
            self.rule_table.delete(iid)
        for rule in self.rules:
            expression = next((e for e in self.expressions if e.get('file') == rule.get('expression_file')), None)
            name = expression.get('name', rule.get('expression_file', '?')) if expression else rule.get('expression_file', '?')
            conditions = rule.get('conditions', [])
            labels = [
                f'{c.get("source")} ≤ {c.get("on_threshold")} / ≥ {c.get("off_threshold")}'
                if c.get('direction') == 'low'
                else f'{c.get("source")} ≥ {c.get("on_threshold")} / ≤ {c.get("off_threshold")}'
                for c in conditions
            ]
            is_active = False
            if self.client:
                with self.client.rules_lock:
                    is_active = self.client.rule_states.get(rule.get('id'), False)
            self.rule_table.insert('', 'end', iid=rule['id'], values=('ON' if rule.get('enabled', True) else 'OFF', name, 'AND' if rule.get('mode', 'all') == 'all' else 'OR', ', '.join(labels), 'ON' if is_active else 'OFF'))
            continue

    def _add_condition(self):
        source = self.rule_source.get()
        try:
            on_threshold = float(self.rule_on.get())
            off_threshold = float(self.rule_off.get())
        except ValueError:
            messagebox.showerror('閾値エラー', 'ON/OFF閾値には数値を入力してください。')
            return
        if not source:
            messagebox.showerror('設定不足', '監視パラメータを選んでください。')
            return
        direction = self.rule_direction.get()
        if direction == 'high' and off_threshold >= on_threshold:
            messagebox.showerror('閾値エラー', '『以上でON』の場合、OFF閾値はON閾値より小さくしてください。')
            return
        if direction == 'low' and off_threshold <= on_threshold:
            messagebox.showerror('閾値エラー', '『以下でON』の場合、OFF閾値はON閾値より大きくしてください。')
            return
        if self.editing_condition_index is None:
            if any((condition['source'] == source for condition in self.builder_conditions)):
                messagebox.showerror('重複条件', '同じパラメータは1つのルール内に1回だけ追加できます。')
                return
        condition = {'source': source, 'direction': direction, 'on_threshold': on_threshold, 'off_threshold': off_threshold}
        if self.editing_condition_index is not None:
            if any((c['source'] == source for i, c in enumerate(self.builder_conditions) if i == self.editing_condition_index)):
                messagebox.showerror('重複条件', '同じパラメータは1つのルール内に1回だけ追加できます。')
                return
            self.builder_conditions[self.editing_condition_index] = condition
            self.editing_condition_index = None
            self.add_condition_button.configure(text='条件を追加')
        else:
            self.builder_conditions.append(condition)
        self._render_conditions()

    def _render_conditions(self):
        for iid in self.condition_table.get_children():
            self.condition_table.delete(iid)
        for index, condition in enumerate(self.builder_conditions):
            self.condition_table.insert('', 'end', iid=str(index), values=(condition['source'], '以下でON' if condition.get('direction') == 'low' else '以上でON', condition['on_threshold'], condition['off_threshold']))

    def _remove_condition(self):
        selected = self.condition_table.selection()
        if selected:
            index = int(selected[0])
            self.builder_conditions.pop(index)
            if self.editing_condition_index == index:
                self.editing_condition_index = None
                self.add_condition_button.configure(text='条件を追加')
            elif self.editing_condition_index is not None and self.editing_condition_index > index:
                self.editing_condition_index -= 1
            self._render_conditions()

    def _begin_edit_condition(self, event):
        iid = self.condition_table.identify_row(event.y)
        if not iid:
            return
        index = int(iid)
        condition = self.builder_conditions[index]
        self.editing_condition_index = index
        self.rule_source.set(condition['source'])
        self.rule_direction.set(condition.get('direction', 'high'))
        self.rule_on.set(str(condition['on_threshold']))
        self.rule_off.set(str(condition['off_threshold']))
        self.add_condition_button.configure(text='条件を更新')

    def _add_rule(self):
        expression = next((e for e in self.expressions if self._expression_label(e) == self.rule_expression.get()), None)
        if not self.builder_conditions or not expression:
            messagebox.showerror('設定不足', '1つ以上の条件と表情差分を設定してください。')
            return
        new_rule = {'id': self.editing_rule_id or str(uuid.uuid4()), 'enabled': True, 'expression_file': expression['file'], 'mode': self.rule_mode.get(), 'conditions': [dict(c) for c in self.builder_conditions]}
        if self.editing_rule_id:
            existing = next((i for i, rule in enumerate(self.rules) if rule.get('id') == self.editing_rule_id), None)
            if existing is not None:
                new_rule['enabled'] = self.rules[existing].get('enabled', True)
                self.rules[existing] = new_rule
            self._finish_edit()
        else:
            self.rules.append(new_rule)
            self.builder_conditions = []
            self._render_conditions()
        self._sync_rules()

    def _begin_edit(self):
        selected = self.rule_table.selection()
        if not selected:
            messagebox.showinfo('ルール編集', '編集するルールを選択してください。')
            return
        rule = next((item for item in self.rules if item.get('id') == selected[0]), None)
        if not rule:
            return
        self.editing_rule_id = rule['id']
        self.builder_conditions = [dict(condition) for condition in rule.get('conditions', [])]
        self.rule_mode.set(rule.get('mode', 'all'))
        expression = next((item for item in self.expressions if item.get('file') == rule.get('expression_file')), None)
        if not expression:
            if rule.get('expression_file'):
                filename = rule['expression_file']
                expression = {'file': filename, 'name': Path(filename).name.removesuffix('.exp3.json')}
                self.expressions.append(expression)
                self._update_rule_choices()
        self.rule_expression.set(self._expression_label(expression) if expression else '')
        self._render_conditions()
        self.rule_editor_label.configure(text='表情切替ルールを編集')
        self.save_rule_button.configure(text='変更を保存')
        self.cancel_edit_button.configure(state='normal')

    def _finish_edit(self):
        self.editing_rule_id = None
        self.editing_condition_index = None
        self.builder_conditions = []
        self._render_conditions()
        self.add_condition_button.configure(text='条件を追加')
        self.rule_editor_label.configure(text='表情切替ルールを追加')
        self.save_rule_button.configure(text='切替ルールを追加')
        self.cancel_edit_button.configure(state='disabled')

    def _cancel_edit(self):
        if self.editing_rule_id:
            self._finish_edit()

    def _remove_rule(self):
        selected = set(self.rule_table.selection())
        if selected:
            self.rules = [rule for rule in self.rules if rule.get('id') not in selected]
            if self.editing_rule_id in selected:
                self._finish_edit()
            self._sync_rules()

    def _toggle_rule(self, event):
        column = self.rule_table.identify_column(event.x)
        iid = self.rule_table.identify_row(event.y)
        if not iid:
            return
        if column != '#1':
            self.rule_table.selection_set(iid)
            self._begin_edit()
            return
        for rule in self.rules:
            if rule.get('id') == iid:
                rule['enabled'] = not rule.get('enabled', True)
                break
        self._sync_rules()

    def _ui(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill='x')
        ttk.Label(top, text='VTS WebSocket URL').pack(side='left')
        ttk.Entry(top, textvariable=self.url, width=30).pack(side='left', padx=6)
        self.connect_button = ttk.Button(top, text='接続', command=self.toggle_connection)
        self.connect_button.pack(side='left')
        ttk.Label(top, textvariable=self.status).pack(side='left', padx=12)
        ttk.Label(self.root, textvariable=self.model_status, padding=(12, 0)).pack(anchor='w')
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill='both', expand=True, padx=10, pady=8)
        self.main_tab = ttk.Frame(self.notebook, padding=10)
        self.params_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.main_tab, text='自動切替')
        self.notebook.add(self.params_tab, text='パラメータ確認')
        info = ttk.LabelFrame(self.main_tab, text='VTS状態', padding=10)
        info.pack(fill='x', pady=(0, 10))
        ttk.Label(info, text='顔トラッキング:').grid(row=0, column=0, sticky='w')
        ttk.Label(info, textvariable=self.face_status).grid(row=0, column=1, sticky='w', padx=4)
        profile_frame = ttk.LabelFrame(self.main_tab, text='全モデル共通プロファイル', padding=8)
        profile_frame.pack(fill='x', pady=(0, 10))
        ttk.Label(profile_frame, textvariable=self.profile_model).pack(side='left', padx=(0, 10))
        ttk.Label(profile_frame, text='使用プロファイル').pack(side='left')
        self.profile_combo = ttk.Combobox(profile_frame, textvariable=self.profile_choice, state='readonly', width=18)
        self.profile_combo.pack(side='left', padx=5)
        self.profile_combo.bind('<<ComboboxSelected>>', self._select_profile)
        ttk.Button(profile_frame, text='新規', command=self._create_profile).pack(side='left', padx=2)
        ttk.Button(profile_frame, text='複製', command=(lambda: self._create_profile(duplicate=True))).pack(side='left', padx=2)
        ttk.Button(profile_frame, text='削除', command=self._delete_profile).pack(side='left', padx=2)
        ttk.Label(profile_frame, text='プロファイル名').pack(side='left')
        ttk.Entry(profile_frame, textvariable=self.profile_name, width=24).pack(side='left', padx=6)
        ttk.Button(profile_frame, text='名前と設定を保存', command=self._save_current_profile).pack(side='left')
        ttk.Label(profile_frame, text='モデルを変えても選択中の設定を維持', foreground='#555').pack(side='left', padx=8)
        self.rule_editor_label = ttk.LabelFrame(self.main_tab, text='表情切替ルールを追加', padding=10)
        editor = self.rule_editor_label
        editor.pack(fill='x', pady=(0, 10))
        ttk.Label(editor, text='監視パラメータ').grid(row=0, column=0, sticky='w')
        self.source_combo = ttk.Combobox(editor, textvariable=self.rule_source, state='readonly', width=24)
        self.source_combo.grid(row=1, column=0, padx=(0, 10), sticky='w')
        ttk.Label(editor, text='ON境界').grid(row=0, column=1, sticky='w')
        ttk.Entry(editor, textvariable=self.rule_on, width=10).grid(row=1, column=1, padx=(0, 10), sticky='w')
        ttk.Label(editor, text='OFF境界').grid(row=0, column=2, sticky='w')
        ttk.Entry(editor, textvariable=self.rule_off, width=10).grid(row=1, column=2, padx=(0, 10), sticky='w')
        ttk.Label(editor, text='この条件で表情をONにする方向').grid(row=0, column=3, sticky='w')
        direction_frame = ttk.Frame(editor)
        direction_frame.grid(row=1, column=3, sticky='w', padx=(0, 8))
        ttk.Radiobutton(direction_frame, text='以上', variable=self.rule_direction, value='high').pack(side='left')
        ttk.Radiobutton(direction_frame, text='以下', variable=self.rule_direction, value='low').pack(side='left')
        self.add_condition_button = ttk.Button(editor, text='条件を追加', command=self._add_condition)
        self.add_condition_button.grid(row=1, column=4, sticky='w')
        self.condition_table = ttk.Treeview(editor, columns=('source', 'direction', 'on', 'off'), show='headings', height=3)
        for key, title, width in (('source', '条件パラメータ', 220), ('direction', 'ON条件', 90), ('on', 'ON境界', 90), ('off', 'OFF境界', 90)):
            self.condition_table.heading(key, text=title)
            self.condition_table.column(key, width=width, anchor='w')
        self.condition_table.grid(row=2, column=0, columnspan=5, sticky='ew', pady=(8, 4))
        self.condition_table.bind('<Double-1>', self._begin_edit_condition)
        ttk.Button(editor, text='選択条件を削除', command=self._remove_condition).grid(row=3, column=4, sticky='e')
        ttk.Label(editor, text='条件の組み合わせ').grid(row=4, column=0, sticky='w', pady=(6, 0))
        mode_frame = ttk.Frame(editor)
        mode_frame.grid(row=5, column=0, sticky='w')
        ttk.Radiobutton(mode_frame, text='すべて（AND）', variable=self.rule_mode, value='all').pack(side='left')
        ttk.Radiobutton(mode_frame, text='どれか（OR）', variable=self.rule_mode, value='any').pack(side='left', padx=(8, 0))
        ttk.Label(editor, text='表情差分').grid(row=4, column=1, sticky='w', pady=(6, 0))
        self.expression_combo = ttk.Combobox(editor, textvariable=self.rule_expression, state='readonly', width=24)
        self.expression_combo.grid(row=5, column=1, padx=(0, 10), sticky='w')
        self.save_rule_button = ttk.Button(editor, text='切替ルールを追加', command=self._add_rule)
        self.save_rule_button.grid(row=5, column=3, sticky='e')
        self.cancel_edit_button = ttk.Button(editor, text='編集をキャンセル', command=self._cancel_edit, state='disabled')
        self.cancel_edit_button.grid(row=5, column=4, sticky='e')
        ttk.Label(editor, text='『以上』はON境界以上でON・OFF境界以下で解除。『以下』はON境界以下でON・OFF境界以上で解除します。', foreground='#555').grid(row=6, column=0, columnspan=5, sticky='w', pady=(8, 0))
        rules_frame = ttk.LabelFrame(self.main_tab, text='登録済みルール（有効欄ダブルクリックでON/OFF、他の欄ダブルクリックで編集）', padding=8)
        rules_frame.pack(fill='both', expand=True)
        rule_cols = ('enabled', 'expression', 'mode', 'conditions', 'state')
        self.rule_table = ttk.Treeview(rules_frame, columns=rule_cols, show='headings', height=9)
        for key, title, width in (('enabled', '有効', 55), ('expression', '表情', 140), ('mode', '条件', 55), ('conditions', 'パラメータとON/OFF閾値', 440), ('state', '現在', 100)):
            self.rule_table.heading(key, text=title)
            self.rule_table.column(key, width=width, anchor='w')
        self.rule_table.bind('<Double-1>', self._toggle_rule)
        self.rule_table.pack(fill='both', expand=True)
        rule_buttons = ttk.Frame(rules_frame)
        rule_buttons.pack(fill='x', pady=(6, 0))
        ttk.Button(rule_buttons, text='選択ルールを編集', command=self._begin_edit).pack(side='left')
        ttk.Button(rule_buttons, text='選択ルールを削除', command=self._remove_rule).pack(side='left', padx=6)
        bar = ttk.Frame(self.params_tab)
        bar.pack(fill='x', pady=(0, 6))
        ttk.Label(bar, text='絞り込み').pack(side='left')
        ttk.Entry(bar, textvariable=self.filter_text, width=30).pack(side='left', padx=6)
        self.filter_text.trace_add('write', (lambda *_: self._apply_filter()))
        ttk.Label(bar, text='顔・口・目・眉などの名称で検索できます').pack(side='left', padx=8)
        ttk.Label(bar, textvariable=self.input_updated).pack(side='right', padx=(10, 0))
        ttk.Label(bar, textvariable=self.live_updated).pack(side='right')
        frame = ttk.Frame(self.params_tab)
        frame.pack(fill='both', expand=True)
        columns = ('category', 'value', 'range')
        self.table = ttk.Treeview(frame, columns=columns, show='tree headings', height=20)
        self.table.heading('#0', text='パラメータ')
        self.table.column('#0', width=300)
        for key, title, width in (('category', '分類', 140), ('value', '値', 150), ('range', '範囲', 150)):
            self.table.heading(key, text=title)
            self.table.column(key, width=width, anchor='w')
        self.table.insert('', 'end', iid='group:input', text='VTS入力パラメータ', open=True, values=('', '', ''))
        self.table.insert('', 'end', iid='group:model', text='Live2Dモデル値', open=True, values=('', '', ''))
        scroll = ttk.Scrollbar(frame, orient='vertical', command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        footer = ttk.Label(self.root, text='VTSのAPI設定で『プラグインAPIを許可』を有効にしてください。', padding=8)
        footer.pack(fill='x')
        self._refresh_profile_choices()
        self._render_rules()

    def toggle_connection(self):
        if self.client:
            self.client.stop()
            self.client = None
            self.connect_button.configure(text='接続')
            self.status.set('切断中')
            return
        parsed = urlparse(self.url.get().strip())
        if parsed.scheme != 'ws' or not parsed.hostname:
            messagebox.showerror('接続先エラー', 'VTSに記載されている ws://127.0.0.1:8001 のような形式で入力してください。')
            return
        self._save_profiles()
        self.client = VTSClient(self.url.get().strip(), self.events)
        self.client.set_rules(self.rules)
        self.connection_failed = False
        self.client.start()
        self.connect_button.configure(text='切断')
        self.status.set('接続中…')

    @staticmethod
    def _category(name: str) -> str:
        low = name.lower()
        if any((k in low for k in ('voice', 'mouth', 'lip', 'talk'))):
            return '口・音声関連'
        if any((k in low for k in ('eye', 'brow', 'eyebrow'))):
            return '目・眉'
        if any((k in low for k in ('face', 'angle', 'position', 'rotation'))):
            return '顔・頭'
        return 'その他'

    def _put_parameters(self, values, source):
        seen_ids = set()
        for p in values or []:
            name = str(p.get('name', ''))
            if not name:
                continue
            if 'value' not in p:
                continue
            item_id = f'{source}:{name}'
            parent = 'group:input' if source == '入力' else 'group:model'
            shown_value = f'{float(p["value"]):.4f}'
            low, high = p.get('min'), p.get('max')
            shown_range = f'{low} ～ {high}' if low is not None and high is not None else '—'
            vals = self._category(name), shown_value, shown_range
            seen_ids.add(item_id)
            if item_id in self.rows:
                self.table.item(item_id, text=name, values=vals)
                continue
            self.table.insert(parent, 'end', iid=item_id, text=name, values=vals)
            self.rows[item_id] = name, parent
            continue
        for item_id in list(self.rows):
            if item_id.startswith(f'{source}:') and item_id not in seen_ids:
                self.table.delete(item_id)
                del self.rows[item_id]
        self._apply_filter()

    def _apply_filter(self):
        query = self.filter_text.get().strip().lower()
        for iid, (name, parent) in self.rows.items():
            visible = not query or query in name.lower() or query in str(self.table.item(iid, 'values')).lower()
            if visible:
                self.table.reattach(iid, parent, 'end')
                if query:
                    self.table.item(parent, open=True)
            else:
                self.table.detach(iid)

    def _drain_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == 'status':
                    self.status.set(payload)
                    if payload == 'VTSに接続しました':
                        self.connection_failed = False
                        self.connect_button.configure(text='切断')
                elif kind == 'error':
                    self.status.set(payload)
                    if str(payload).startswith('接続エラー:'):
                        self.connection_failed = True
                elif kind == 'live':
                    self.model_status.set(f'モデル: {payload.get("modelName") or "未ロード"}  |  Live2Dパラメータ: {len(payload.get("parameters", []))}件')
                    if payload.get('modelLoaded') and payload.get('modelID'):
                        self._switch_model_profile(payload['modelID'], payload.get('modelName', ''))
                    self._put_parameters(payload.get('parameters', []), 'モデル')
                    self.live_updated.set(f'Live2D値: {datetime.now().strftime("%H:%M:%S")}')
                elif kind == 'inputs':
                    values = payload.get('defaultParameters', []) + payload.get('customParameters', [])
                    self._put_parameters(values, '入力')
                    self._update_rule_choices(payload)
                    self.input_updated.set(f'VTS入力: {datetime.now().strftime("%H:%M:%S")}')
                elif kind == 'expressions':
                    self.expressions = payload or []
                    self._update_rule_choices()
                elif kind == 'rule_state':
                    rule_id, active = payload
                    if self.rule_table.exists(rule_id):
                        self.rule_table.set(rule_id, 'state', 'ON' if active else 'OFF')
                elif kind == 'face':
                    self.face_status.set('検出中' if payload else '未検出')
                elif kind == 'disconnected' and self.client:
                    self.client = None
                    if self.connection_failed:
                        self.connect_button.configure(text='再接続')
                    else:
                        self.connect_button.configure(text='接続')
                        self.status.set('未接続')
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    def close(self):
        if self.client:
            self.client.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    FeatureMonitor(root)
    root.mainloop()

if __name__ == '__main__':
    main()
