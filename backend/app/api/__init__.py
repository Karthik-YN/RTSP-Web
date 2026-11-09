from . import analytics, auth, groups, streams, system, videos

routers = [auth.router, auth.users_router, videos.router, groups.router, analytics.router, streams.router, system.router]
