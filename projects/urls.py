from rest_framework.routers import SimpleRouter
from .views import ProjectViewSet

router = SimpleRouter()
router.register(r'', ProjectViewSet, basename='project')

urlpatterns = router.urls