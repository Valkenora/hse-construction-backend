from rest_framework.routers import SimpleRouter
from .views import PermitViewSet

router = SimpleRouter()
router.register(r'', PermitViewSet, basename='permit')

urlpatterns = router.urls