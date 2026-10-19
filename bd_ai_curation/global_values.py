debug = False
bd = None  # blackduck.Client instance, set by rest_client.connect()

bd_project = ''
bd_version = ''
bd_api = ''
bd_url = ''
bd_trustcert = False


anthropic_base_url = ''
anthropic_api_key = ''
llm_model = 'claude-haiku-5-5'

apply = False
include_match_review = False
unconfirmed_snippets = False
filter_paths = False
max_components_per_llm_call = 20
report_file = ''
decisions_file = ''

max_concurrent_rest_requests = 10
max_concurrent_llm_calls = 5
