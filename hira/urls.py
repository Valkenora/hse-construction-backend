from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import HIRAActivityViewSet, HIRADocumentViewSet, HIRAGroupViewSet

router = DefaultRouter()
router.register(r'documents', HIRADocumentViewSet, basename='hira-document')
router.register(r'groups', HIRAGroupViewSet, basename='hira-group')
router.register(r'activities', HIRAActivityViewSet, basename='hira-activity')

urlpatterns = [path('', include(router.urls))]