from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("accounts/signup/", views.signup, name="signup"),
    path("api/aapl/chart/", views.aapl_chart, name="aapl_chart"),
    path("api/symbol-search/", views.symbol_search, name="symbol_search"),
    path("api/portfolio/", views.portfolio, name="portfolio"),
    path("api/buy-stock/", views.buy_stock, name="buy_stock"),
]

