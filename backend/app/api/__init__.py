from . import analytics, auth, groups, health, naming, streams, system, videos

routers = [auth.router, auth.users_router, naming.router, videos.router, groups.router, analytics.router, streams.router, system.router, health.router]
