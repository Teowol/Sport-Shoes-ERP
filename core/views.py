from django import forms
from django.contrib.auth import login
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import Group
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import redirect, render

from distribution.models import Customer
from .models import Employee
from .permissions import can_view_employees
from .templatetags.module_navigation import resolve_module_links

User = get_user_model()


def healthz(request):
    """Load balancer and deployment health-check endpoint."""
    return JsonResponse({"status": "ok"})


def is_buyer(user):
    """Kullanıcının Müşteri (Buyer) olup olmadığını kontrol eder."""
    return user.is_authenticated and user.groups.filter(name="Buyer").exists()


def is_factory_user(user):
    """Kullanıcının Fabrika Sahibi / Personel olup olmadığını kontrol eder."""
    return user.is_authenticated and (
        user.is_superuser or user.is_staff or user.groups.filter(name="FactoryOwner").exists()
    )


class CustomerRegisterForm(UserCreationForm):
    email = forms.EmailField(required=True, label="E-posta")
    first_name = forms.CharField(max_length=150, required=True, label="Ad")
    last_name = forms.CharField(max_length=150, required=True, label="Soyad")
    phone = forms.CharField(max_length=30, required=True, label="Telefon")
    address = forms.CharField(
        required=True,
        label="Adres",
        widget=forms.Textarea(attrs={"rows": 4}),
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = (
            "username",
            "first_name",
            "last_name",
            "email",
            "password1",
            "password2",
        )

    @transaction.atomic
    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        user.first_name = self.cleaned_data["first_name"]
        user.last_name = self.cleaned_data["last_name"]

        if commit:
            user.save()
            buyer_group, _ = Group.objects.get_or_create(name="Buyer")
            user.groups.add(buyer_group)

            Customer.objects.create(
                user=user,
                code=f"CUST-{user.pk:05d}",
                name=f"{user.first_name} {user.last_name}".strip(),
                email=user.email,
                phone=self.cleaned_data["phone"],
                address=self.cleaned_data["address"],
            )
        return user


def home(request):
    """
    Ana Karşılama Sayfası:
    - Giriş yapılmamışsa: Rol seçim kartlarını gösterir.
    - Giriş yapılmışsa: Rolüne göre doğrudan doğru panele yönlendirir.
    """
    if request.user.is_authenticated:
        if is_buyer(request.user):
            return redirect("customer_home")
        return redirect("portal")
    return render(request, "core/home.html", {
        "module_links": resolve_module_links(),
    })


@login_required
def portal(request):
    """
    Fabrika Sahibi / Yönetim Portalı:
    - Müşteri buraya girmeye çalışırsa otomatik olarak müşteri paneline yönlendirilir.
    """
    if is_buyer(request.user):
        return redirect("customer_home")
    return render(request, "core/portal.html", {
        "can_view_employees": can_view_employees(request.user),
        "module_links": resolve_module_links(),
    })


@login_required
def employee_list(request):
    if not can_view_employees(request.user):
        raise PermissionDenied
    query = request.GET.get("q", "").strip()
    employees = Employee.objects.all()
    if query:
        employees = employees.filter(
            Q(code__icontains=query) | Q(full_name__icontains=query)
            | Q(profession__icontains=query) | Q(department__icontains=query)
        )
    page = Paginator(employees, 25).get_page(request.GET.get("page"))
    return render(request, "core/employee_list.html", {"page_obj": page, "query": query})


@login_required
def customer_home(request):
    """
    Müşteri Portalı:
    - Fabrika sahibi buraya girerse kendi portalına yönlendirilir.
    """
    if not is_buyer(request.user):
        return redirect("portal")

    customer, _ = Customer.objects.get_or_create(
        user=request.user,
        defaults={
            "code": f"CUST-{request.user.pk:05d}",
            "name": f"{request.user.first_name} {request.user.last_name}".strip() or request.user.username,
            "email": request.user.email,
        },
    )

    context = {
        "customer": customer,
        "full_name": customer.name,
        "email": customer.email,
        "phone": customer.phone,
        "address": customer.address,
        "customer_code": customer.code,
    }
    return render(request, "core/customer_home.html", context)


def register(request):
    if request.user.is_authenticated:
        return redirect("home")

    if request.method == "POST":
        form = CustomerRegisterForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            return redirect("customer_home")
    else:
        form = CustomerRegisterForm()

    return render(request, "registration/register.html", {"form": form})
