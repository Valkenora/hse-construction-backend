from django.urls import path

from .mk_api import MKApproveView, MKDetailView, MKRejectView, MKSendOTPView

urlpatterns = [
    path('<slug:token>/', MKDetailView.as_view()),
    path('<slug:token>/send-otp/', MKSendOTPView.as_view()),
    path('<slug:token>/approve/', MKApproveView.as_view()),
    path('<slug:token>/reject/', MKRejectView.as_view()),
]
