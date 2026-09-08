from karnex_proxy import create_proxy_app

app = create_proxy_app(
    title="Karnex Candidate Service",
    service_id="candidate-service",
    path_prefixes=("/hr/candidates", "/candidates/ranked"),
)
