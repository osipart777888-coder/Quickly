"""Open the parser's own persistent Chrome for manual directory sign-in."""
import argparse
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--parser-root', type=Path, required=True)
    parser.add_argument('--source', choices=['spark', 'rusprofile'], required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.parser_root))
    from company_emails.runner import run_lock
    from company_emails.sources import LOCAL, SOURCES
    from company_emails.spark_browser import SparkBrowser
    from company_emails.rusprofile_browser import RusprofileBrowser
    from company_emails.checkpoints import atomic_json
    factory = SparkBrowser if args.source == 'spark' else RusprofileBrowser
    try:
        with run_lock(LOCAL / 'profiles' / args.source):
            with factory() as browser:
                browser.load(SOURCES[args.source]['home'])
        result = {'status': 'opened', 'message': 'Войдите в отдельном окне Chrome и вставьте ссылку выборки в Quickly. Не закрывайте это окно до завершения сбора.'}
    except Exception:
        result = {'status': 'error', 'message': 'Не удалось открыть окно входа. Дождитесь окончания сбора и повторите.'}
    atomic_json(LOCAL / 'quickly-access-result.json', result)


if __name__ == '__main__': main()
