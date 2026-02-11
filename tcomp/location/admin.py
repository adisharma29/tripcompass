from django.contrib import admin
from .models import Country, State, District, City


@admin.register(Country)
class CountryAdmin(admin.ModelAdmin):
    list_display = ['name', 'code']
    search_fields = ['name']


@admin.register(State)
class StateAdmin(admin.ModelAdmin):
    list_display = ['name', 'country']
    list_filter = ['country']
    search_fields = ['name']


@admin.register(District)
class DistrictAdmin(admin.ModelAdmin):
    list_display = ['name', 'state', 'is_multipoly']
    list_filter = ['state__country', 'state']
    search_fields = ['name']


@admin.register(City)
class CityAdmin(admin.ModelAdmin):
    list_display = ['name', 'district', 'state', 'population']
    list_filter = ['state']
    search_fields = ['name']
