"""One isolated capture helper; stdout carries bounded structured events only."""
from services.operation_diagnostics import worker_diagnostics
import json
import sys


@worker_diagnostics
def main(arguments):
    if arguments:
        return 2
    try:
        from modules.twitter.acquisition import capture
        spec = json.loads(sys.stdin.readline(16384))
        def notify(phase, progress):
            print(json.dumps({'event': 'progress', 'phase': phase, 'progress': progress}), flush=True)
        def media_notify(name,progress):
            print(json.dumps({'event':'file','name':name,'progress':progress}),flush=True)
        files = capture(spec, notify, media_notify=media_notify)
        print(json.dumps({'event': 'complete', 'files': files}), flush=True)
        return 0
    except Exception as error:
        print(json.dumps({'event': 'failure', 'error': f'{type(error).__name__}: {error}'[:1000]}), flush=True)
        return 1
