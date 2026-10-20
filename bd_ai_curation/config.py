import os
import argparse
import sys
import logging

from . import global_values

parser = argparse.ArgumentParser(description='Black Duck AI Curation Service',
                                  prog='bd-ai-curate')

parser.add_argument('project', help='Black Duck project name')
parser.add_argument('version', help='Black Duck project version name')
parser.add_argument("--blackduck_url", type=str, help="Black Duck server URL (REQUIRED, or set BLACKDUCK_URL)",
                     default="")
parser.add_argument("--blackduck_api_token", type=str,
                     help="Black Duck API token (REQUIRED, or set BLACKDUCK_API_TOKEN)", default="")
parser.add_argument("--blackduck_trust_cert", help="Black Duck trust server cert", action='store_true')
parser.add_argument("--debug", help="Debug logging mode", action='store_true')
parser.add_argument("--logfile", help="Logging output file", default="")
parser.add_argument("--update_bom", help="Apply proposed decisions to the BOM (default is dry-run/report only)",
                        action='store_true')
parser.add_argument("--include_match_review",
                        help="Also fetch and curate Match Review (snippet/low-match-score and unmatched) components",
                        action='store_true')
parser.add_argument("--include_unconfirmed_snippets",
                        help="Also curate unconfirmed (NOT_REVIEWED) snippet matches, grouped by the reported "
                             "component/version. Strong matches are confirmed (added to the BOM and marked "
                             "REVIEWED) only with --update_bom; all others are left for manual review",
                        action='store_true')
parser.add_argument("--filter_paths",
                        help="Strip known-noise matched file paths (tests, Coverity 'java-api-stubs.jar', "
                             "Detect run folders/jars, virtualenv folders, hidden dot-folders) before curation; "
                             "a component left with no real evidence is auto-ignored/rejected without an LLM call",
                        action='store_true')
parser.add_argument("--max_components_per_llm_call", type=int, default=20,
                        help="Max number of components/groups to send to the LLM per call (default 20)")
parser.add_argument("--max_concurrent_rest_requests", type=int, default=10,
                        help="Max number of concurrent Black Duck REST calls (default 10)")
parser.add_argument("--max_concurrent_llm_calls", type=int, default=5,
                        help="Max number of concurrent Anthropic LLM calls (default 5)")
parser.add_argument("--model", type=str, default='claude-haiku-5-5',
                        help="Anthropic model to use for curation decisions")
parser.add_argument("--report_file", help="Text report output file", default="")
parser.add_argument("--decisions_file", help="JSON decision log output file (default: bd_ai_curation_decisions.json)",
                        default="bd_ai_curation_decisions.json")

args = parser.parse_args()


def check_args():
    if args.debug:
        global_values.debug = True
        loglevel = logging.DEBUG
    else:
        loglevel = logging.INFO

    if args.logfile != '':
        if os.path.exists(args.logfile):
            logging.error(f"Specified logfile '{args.logfile}' already exists - EXITING")
            sys.exit(2)
        logging.basicConfig(encoding='utf-8',
                             handlers=[logging.FileHandler(args.logfile), logging.StreamHandler(sys.stdout)],
                             level=loglevel)
    else:
        logging.basicConfig(level=loglevel)

    logging.info("ARGUMENTS:")
    for arg in vars(args):
        logging.info(f"--{arg}={getattr(args, arg)}")
    logging.info('')

    terminate = False

    url = os.environ.get('BLACKDUCK_URL') or os.environ.get('BLACKDUCK_BDSCA_URL')
    if args.blackduck_url != '':
        global_values.bd_url = args.blackduck_url
    elif url is not None:
        global_values.bd_url = url
    else:
        logging.error("Black Duck URL not specified (use --blackduck_url or set BLACKDUCK_URL)")
        terminate = True

    api = os.environ.get('BLACKDUCK_API_TOKEN') or os.environ.get('BLACKDUCK_BDSCA_TOKEN')
    if args.blackduck_api_token != '':
        global_values.bd_api = args.blackduck_api_token
    elif api is not None:
        global_values.bd_api = api
    else:
        logging.error("Black Duck API Token not specified (use --blackduck_api_token or set BLACKDUCK_API_TOKEN)")
        terminate = True

    trustcert = os.environ.get('BLACKDUCK_TRUST_CERT')
    if trustcert == 'true' or args.blackduck_trust_cert:
        global_values.bd_trustcert = True

    global_values.bd_project = args.project
    global_values.bd_version = args.version

    global_values.anthropic_base_url = os.environ.get('ANTHROPIC_BASE_URL', '')
    global_values.anthropic_api_key = (os.environ.get('ANTHROPIC_AUTH_TOKEN')
                                        or os.environ.get('ANTHROPIC_API_KEY'))
    if not global_values.anthropic_api_key:
        logging.error("Anthropic credentials not specified "
                       "(set ANTHROPIC_AUTH_TOKEN/ANTHROPIC_API_KEY)")
        terminate = True

    global_values.apply = args.update_bom
    global_values.include_match_review = args.include_match_review
    global_values.unconfirmed_snippets = args.include_unconfirmed_snippets
    global_values.filter_paths = args.filter_paths
    global_values.max_components_per_llm_call = args.max_components_per_llm_call
    global_values.max_concurrent_rest_requests = args.max_concurrent_rest_requests
    global_values.max_concurrent_llm_calls = args.max_concurrent_llm_calls
    global_values.llm_model = args.model
    global_values.report_file = args.report_file
    global_values.decisions_file = args.decisions_file

    if args.report_file and os.path.exists(args.report_file):
        logging.error(f"Report file '{args.report_file}' already exists - exiting")
        terminate = True

    if terminate:
        sys.exit(2)
    return
