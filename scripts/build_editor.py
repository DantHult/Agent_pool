"""Rebuild the locally vendored Monaco editor; no CDN or runtime npm required."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from relay.native_environment import clean_environment, toolchain  # noqa: E402


def main():
    runtime = toolchain()
    if not runtime:
        raise SystemExit('Prepare the approved local Node/npm toolchain first')
    output = Path(__file__).resolve().parents[1] / 'static/vendor/monaco'
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='relay-editor-build-') as directory:
        stage = Path(directory)
        home = stage / '.native/home'
        home.mkdir(parents=True)
        (stage / '.tmp').mkdir()
        environment = clean_environment(home, runtime)
        environment.pop('NPM_CONFIG_OFFLINE')
        (stage / 'package.json').write_text('{"private":true,"dependencies":{"monaco-editor":"0.57.0","esbuild":"0.28.2"}}')
        subprocess.run([runtime / 'bin/node', runtime / 'package/bin/npm-cli.js', 'install', '--ignore-scripts', '--no-audit', '--no-fund', '--registry=https://registry.npmjs.org'], cwd=stage, env=environment, stdin=subprocess.DEVNULL, check=True, timeout=180)
        languages = ['javascript', 'typescript', 'python', 'html', 'css', 'markdown', 'shell', 'yaml', 'sql']
        entry = "import * as monaco from 'monaco-editor/editor/editor.api.js';\n"
        for language in languages:
            entry += "import * as " + language + "Grammar from 'monaco-editor/languages/definitions/" + language + '/' + language + ".js';\n"
            entry += "monaco.languages.register({id:'" + language + "'});monaco.languages.setMonarchTokensProvider('" + language + "'," + language + "Grammar.language);monaco.languages.setLanguageConfiguration('" + language + "'," + language + "Grammar.conf);\n"
        entry += "globalThis.MonacoEnvironment={getWorker(){return new Worker('/vendor/monaco/editor.worker.js',{type:'module',name:'relay-editor'});}};\n"
        entry += "globalThis.RelayMonaco=monaco; globalThis.dispatchEvent(new Event('relay-monaco-ready'));\n"
        (stage / 'editor.js').write_text(entry)
        esbuild = [runtime / 'bin/node', stage / 'node_modules/esbuild/bin/esbuild']
        options = ['--bundle', '--format=esm', '--minify', '--target=es2022', '--legal-comments=eof']
        subprocess.run([*esbuild, stage / 'editor.js', *options, '--loader:.ttf=file', '--asset-names=assets/[name]-[hash]', '--outfile=' + str(output / 'editor.js')], cwd=stage, env=environment, stdin=subprocess.DEVNULL, check=True)
        subprocess.run([*esbuild, stage / 'node_modules/monaco-editor/esm/vs/editor/editor.worker.js', *options, '--outfile=' + str(output / 'editor.worker.js')], cwd=stage, env=environment, stdin=subprocess.DEVNULL, check=True)
        for name in ('LICENSE', 'ThirdPartyNotices.txt'):
            source = stage / 'node_modules/monaco-editor' / name
            if source.exists():
                shutil.copyfile(source, output / name)
        shutil.copyfile(stage / 'package-lock.json', output / 'build-lock.json')
        (output / 'VERSION').write_text(json.dumps({'monaco-editor': '0.57.0', 'esbuild': '0.28.2', 'source': 'https://github.com/microsoft/monaco-editor'}, indent=2) + '\n')
    print('Built local Monaco assets in', output)


if __name__ == '__main__':
    main()
