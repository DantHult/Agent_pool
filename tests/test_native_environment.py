"""Offline regressions. These do not claim real model or package execution."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from relay import native_environment as env
from relay.app_server import server_command
from relay.native import NativeCodexAdapter, initialize_profiles, thread_options
from relay.workspace import Workspace


@unittest.skipUnless(hasattr(os, 'getuid'), 'Native environment ownership checks require a POSIX user id')
class NativeEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name).resolve()
        self.patch = patch.dict(os.environ, {'RELAY_NATIVE_ROOT': str(self.directory / 'native'),
                                             'OPENAI_API_KEY': 'not-for-tools', 'PATH': '/private/unapproved/bin:/usr/bin:/bin'})
        self.patch.start()
        self.ws = Workspace(self.directory / 'workspaces')
        self.workspace = self.ws.path('project')

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def approve(self):
        tools = env.private_directory(env.root() / 'tools')
        runtime = tools / 'runtime'
        (runtime / 'bin').mkdir(parents=True)
        (runtime / 'bin/node').write_text('approved-fixture')
        env.write_json(tools / 'approved.json', {'uid': os.getuid(), 'digest': env.tree_digest(runtime)})
        return runtime

    def package(self):
        (self.workspace / 'frontend').mkdir()
        (self.workspace / 'frontend/package.json').write_text('{"dependencies":{"react":"^19.0.0"}}')
        self.ws.checkpoint('project', 'Package manifest')

    def bundle(self):
        files = env.manifests(self.workspace)
        key = env.bundle_key(files)
        bundle = env.private_directory(env.root() / 'environments' / key)
        payload = bundle / 'payload'
        (payload / 'frontend/node_modules/react').mkdir(parents=True)
        (payload / 'frontend/node_modules/react/index.js').write_text('export default "fixture";')
        (payload / 'frontend/package-lock.json').write_text('{"lockfileVersion":3,"packages":{}}')
        env.write_json(bundle / 'bundle.json', {'files': files, 'key': key, 'requirements': False, 'digest': env.tree_digest(payload)})
        return bundle

    def test_path_is_explicit_and_no_auth_or_user_startup_config_is_forwarded(self):
        runtime = self.approve()
        params = thread_options(self.workspace)
        variables = params['config']['shell_environment_policy.set']
        self.assertTrue(variables['PATH'].startswith(str(self.workspace / '.venv/bin') + ':' + str(runtime / 'bin')))
        self.assertNotIn('unapproved', variables['PATH'])
        self.assertNotIn('OPENAI_API_KEY', variables)
        self.assertTrue(Path(variables['ZDOTDIR']).is_relative_to(self.workspace))
        self.assertEqual(variables['PIP_NO_INDEX'], '1')
        self.assertEqual(variables['NPM_CONFIG_IGNORE_SCRIPTS'], 'true')
        self.assertEqual(variables['OPENSSL_CONF'], '/dev/null')
        self.assertNotIn('NODE_TLS_REJECT_UNAUTHORIZED', variables)
        self.assertTrue(Path(variables['TMPDIR']).is_relative_to(self.workspace))

    @unittest.skipUnless(Path('/bin/zsh').is_file(), 'Requires zsh login startup')
    def test_login_shell_uses_private_python_after_system_path_helper(self):
        binary = self.workspace / '.venv/bin/python3'
        binary.parent.mkdir(parents=True)
        binary.write_text('#!/bin/sh\nprintf private-python-runtime')
        binary.chmod(0o755)
        variables = env.workspace_environment(self.workspace)
        env.create_shell_profile(self.workspace)
        result = subprocess.run(['/bin/zsh', '-lc', 'python3'], env=variables, cwd=self.workspace,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'private-python-runtime')

    def test_only_approved_runtime_is_readable_and_network_stays_disabled(self):
        runtime = self.approve()
        command = server_command('/usr/bin/true')
        policy = next(arg for arg in command if arg.startswith('permissions.relay-native.filesystem='))
        self.assertIn(json.dumps(str(runtime)) + '="read"', policy)
        self.assertNotIn(str(env.root() / 'profiles'), policy)
        self.assertNotIn(str(env.root() / 'environments'), policy)
        self.assertIn('permissions.relay-native.network.enabled=false', command)
        self.assertIn('approval_policy="never"', command)

    def test_changed_tool_binary_is_rejected(self):
        runtime = self.approve()
        (runtime / 'bin/node').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'changed'):
            env.toolchain()

    def test_workspace_cache_directories_cannot_follow_model_links(self):
        outside = self.directory / 'outside'
        outside.mkdir()
        for component in ('.tmp', '.native'):
            link = self.workspace / component
            link.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(OSError):
                env.workspace_environment(self.workspace)
            link.unlink()
        (self.workspace / '.native').mkdir()
        (self.workspace / '.native/home').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            env.workspace_environment(self.workspace)
        self.assertEqual(list(outside.iterdir()), [])

    def test_shell_startup_profile_cannot_overwrite_a_link_or_existing_file(self):
        env.workspace_environment(self.workspace)
        outside = self.directory / 'outside-file'
        outside.write_text('preserve')
        profile = self.workspace / '.native/home/.zprofile'
        profile.symlink_to(outside)
        with self.assertRaises(FileExistsError):
            env.create_shell_profile(self.workspace)
        self.assertEqual(outside.read_text(), 'preserve')

    def test_incomplete_tool_provision_does_not_break_python_only_tasks(self):
        env.private_directory(env.root() / 'tools')
        self.assertIsNone(env.toolchain())
        self.assertEqual(env.toolchain_signature(), '')
        self.assertTrue(env.hydrate(self.workspace)['ready'])

    def test_supported_node_checks_exact_npm_engine_minimum(self):
        for version in ('v22.22.0', 'v22.22.1', 'v24.14.9', 'v25.0.0', 'v24.22.1-malicious'):
            self.assertFalse(env.supported_node(version), version)
        for version in ('v22.22.2', 'v24.15.0', 'v24.100.0', 'v26.0.0'):
            self.assertTrue(env.supported_node(version), version)

    def test_tool_link_cannot_escape_to_auth_home(self):
        runtime = self.approve()
        outside = self.directory / 'canary'
        outside.write_text('nonsecret')
        (runtime / 'bin/unsafe').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'escapes'):
            env.toolchain()

    def test_artifact_root_symlink_cannot_grant_an_external_directory(self):
        external = self.directory / 'outside-tree'
        external.mkdir()
        (external / 'canary').write_text('nonsecret')
        linked = self.directory / 'artifact'
        linked.symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'regular directory'):
            env.tree_digest(linked)

    def test_missing_dependencies_are_reported_without_network_install(self):
        self.package()
        with patch.object(env, 'run', wraps=env.run) as run:
            status = env.hydrate(self.workspace)
        self.assertFalse(status['ready'])
        self.assertIn('local owner', status['message'])
        self.assertFalse(any('pip' in str(call) or 'npm' in str(call) for call in run.call_args_list))

    def test_requirements_and_package_install_inputs_reject_executable_sources(self):
        for text in ('-r /private/key', '-e .', 'https://example.com/a.whl', 'foo @ https://example.com/foo', '--index-url https://example.com'):
            with self.assertRaises(ValueError):
                env.validate_requirements(text)
        self.assertEqual(env.validate_requirements('Django>=5.2,<5.3\n# comment\n'), ['Django>=5.2,<5.3'])
        for version in ('file:../secret', 'git+https://example.com/repo', 'https://example.com/a.tgz', 'npm:other@latest'):
            with self.assertRaises(ValueError):
                env.validate_package(json.dumps({'dependencies': {'thing': version}}))
        for key in ('overrides', 'workspaces', 'bundledDependencies', 'bundleDependencies'):
            with self.assertRaises(ValueError):
                env.validate_package(json.dumps({key: {'thing': 'file:../secret'}}))

    def test_lock_rejects_remote_and_local_artifacts(self):
        for entry in ({'resolved': 'https://example.com/a', 'integrity': 'sha512-fixture'}, {'link': True}, {'resolved': 'file:../x'}):
            with self.assertRaises(ValueError):
                env.validate_lock(json.dumps({'lockfileVersion': 3, 'packages': {'node_modules/thing': entry}}))

    def test_artifact_tampering_is_rejected(self):
        self.approve()
        self.package()
        bundle = self.bundle()
        (bundle / 'payload/frontend/node_modules/react/index.js').write_text('tampered')
        with self.assertRaisesRegex(ValueError, 'changed'):
            env.hydrate(self.workspace)

    def test_two_workers_hydrate_independent_dependencies_and_continue_after_lock_checkpoint(self):
        self.approve()
        self.package()
        self.bundle()
        initialize_profiles()
        first = NativeCodexAdapter({'native_profile': 'b'})
        second = NativeCodexAdapter({'native_profile': 'c'})
        working, expected = first._prepare(self.workspace)
        self.assertTrue(json.loads((working / '.native/environment.json').read_text())['ready'])
        (working / 'continued.txt').write_text('B finished source work')
        # Dependency trees may exceed checkpoint limits and include normal npm
        # symlinks. They must not be scanned, redacted, copied or committed.
        (working / 'frontend/node_modules/large').write_bytes(b'x' * (Workspace.MAX_FILE_BYTES + 1))
        first._publish(self.workspace, working, expected)
        self.ws.checkpoint('project', 'B source and lock')
        after, parent = second._prepare(self.workspace)
        self.assertTrue(json.loads((after / '.native/environment.json').read_text())['ready'])
        self.assertEqual((after / 'continued.txt').read_text(), 'B finished source work')
        self.assertFalse((after / 'frontend/node_modules/large').exists())
        (after / 'frontend/node_modules/react/index.js').write_text('private C change')
        self.assertNotEqual((working / 'frontend/node_modules/react/index.js').read_text(), 'private C change')
        second._publish(self.workspace, after, parent)
        tracked = self.ws.git(self.workspace, 'ls-files')
        self.assertIn('package-lock.json', tracked)
        self.assertNotIn('node_modules', tracked)
        self.assertNotIn('.native/', tracked)
        self.assertNotIn('.venv/', tracked)

    def test_dependency_change_needs_new_owner_approval(self):
        self.approve()
        self.package()
        self.bundle()
        (self.workspace / 'frontend/package.json').write_text('{"dependencies":{"react":"^20.0.0"}}')
        self.ws.checkpoint('project', 'Changed dependencies')
        self.assertFalse(env.hydrate(self.workspace)['ready'])

    def test_tracked_runtime_collision_cannot_be_overwritten(self):
        (self.workspace / '.venv').mkdir()
        (self.workspace / '.venv/important').write_text('tracked source')
        subprocess.run(['/usr/bin/git', 'add', '-f', '.venv/important'], cwd=self.workspace, check=True, capture_output=True)
        self.ws.checkpoint('project', 'Tracked collision')
        with self.assertRaisesRegex(ValueError, 'collide'):
            env.hydrate(self.workspace)


if __name__ == '__main__':
    unittest.main()
