from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("leaderboard/", views.leaderboard, name="leaderboard"),
    path("trade-history/", views.trade_history, name="trade_history"),
    path("inventory/", views.inventory, name="inventory"),
    path("accounts/signup/", views.signup, name="signup"),
    path("api/stock/chart/", views.stock_chart, name="stock_chart"),
    path("api/stock/news/", views.stock_news, name="stock_news"),
    path("api/symbol-search/", views.symbol_search, name="symbol_search"),
    path("api/portfolio/", views.portfolio, name="portfolio"),
    path("api/sector-breakdown/", views.sector_breakdown, name="sector_breakdown"),
    path("api/buy-stock/", views.buy_stock, name="buy_stock"),
    path("api/sell-stock/", views.sell_stock, name="sell_stock"),
    path("api/apply-stardust-shield/", views.apply_stardust_shield, name="apply_stardust_shield"),
    path(
        "api/apply-multiply-profit-boost/",
        views.apply_multiply_profit_boost,
        name="apply_multiply_profit_boost",
    ),
    path("api/use-recon/", views.use_recon, name="use_recon"),
    path("api/advisor-chat/", views.advisor_chat, name="advisor_chat"),
]

