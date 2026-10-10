"""Context builder workflow Lambdas (WS15, plan ticket T08).

State machine (infra/stacks/context.py): Map over fields -> LoadBoundary -> Parallel(stage Lambdas, each
with a Catch) -> Finalize. Handlers:

    cloud.lambdas.context.load_boundary.handler
    cloud.lambdas.context.stages.<soil|terrain|crop|disease|economics|baseline>_handler
    cloud.lambdas.context.finalize.handler
"""
