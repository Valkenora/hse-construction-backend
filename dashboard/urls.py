from django.urls import path
from .views import DashboardStatsView, DashboardTasksView

urlpatterns = [
    path('stats/', DashboardStatsView.as_view(), name='dashboard-stats'),
    path('tasks/', DashboardTasksView.as_view(), name='dashboard-tasks'),
]
