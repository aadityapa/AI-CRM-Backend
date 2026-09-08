"""Karnex - new interview invitation email format.

Adds the full invitation template (greeting, details table, confirmation
request, recruiter signature) and wires it into the AI interview invite.

    cd F:\\AI-Interview-Model-B-V2
    python karnex_invite_template.py            apply
    python karnex_invite_template.py --check    dry run, changes nothing
    python karnex_invite_template.py --preview  print a sample email

Safe to re-run. No database change, no migration, no restart needed to preview.
"""
from __future__ import annotations

import argparse
import base64
import io
import os
import sys

OK, BAD, WARN = "  [ok]  ", "  [FAIL]", "  [warn]"
CHECK_ONLY = False

#: Optional. A blank value drops its row from the email rather than printing an
#: empty one, so none of these are required to get a clean invitation.
ENV_DEFAULTS = [
    ("COMPANY_NAME", "Karnex Software Solutions PVT LTD"),
    ("COMPANY_SHORT_NAME", "Karnex"),
    ("COMPANY_WEBSITE", "https://karnex.in/"),
    ("COMPANY_EMAIL", "info@karnex.in"),
    ("COMPANY_PHONE", ""),
    ("INTERVIEW_DEFAULT_DURATION", ""),
]

FILES: dict[str, str] = {
    "backend/services/interview_invite_email.py":
        "IiIiVGhlIGNhbmRpZGF0ZS1mYWNpbmcgaW50ZXJ2aWV3IGludml0YXRpb24gZW1haWwuCgpSZXBsYWNlcyB0aGUgc2hvcnQgImhlcmUgaXMgeW"
        "91ciBsaW5rIiBub3RlIHdpdGggYSBwcm9wZXIgaW52aXRhdGlvbjogZ3JlZXRpbmcsCmEgZGV0YWlscyB0YWJsZSwgYSBjb25maXJtYXRpb24g"
        "cmVxdWVzdCwgYW5kIGEgc2lnbmF0dXJlIGJsb2NrIGZvciB0aGUgcmVjcnVpdGVyCndobyBhY3R1YWxseSBzZW50IGl0LgoKVHdvIHJ1bGVzIG"
        "RyaXZlIHRoZSB3aG9sZSBtb2R1bGUuCgoqKk5ldmVyIHJlbmRlciBhbiBlbXB0eSByb3cuKiogQSBkZXRhaWxzIHRhYmxlIHdpdGggIlZlbnVl"
        "IEFkZHJlc3M6IOKAlCIgaW4gaXQKbG9va3MgYnJva2VuLCBhbmQgYW4gQUkgaW50ZXJ2aWV3IGhhcyBubyB2ZW51ZSBhdCBhbGwuIGBfcm93cy"
        "gpYCBkcm9wcyBhbnl0aGluZwp3aXRob3V0IGEgdmFsdWUsIHNvIHRoZSB0YWJsZSBvbmx5IGV2ZXIgc2hvd3MgZmFjdHMgd2UgaG9sZC4gVGhh"
        "dCBpcyB3aGF0IG1ha2VzCnRoZSBzYW1lIHRlbXBsYXRlIHdvcmsgZm9yIGFuIG9ubGluZSBBSSBzY3JlZW5pbmcgYW5kIGFuIGluLXBlcnNvbi"
        "BMMiByb3VuZC4KCioqVGhlIHNpZ25hdHVyZSBpcyB0aGUgcGVyc29uLCBub3QgdGhlIHByb2R1Y3QuKiogYHNlbmRlcl9kZXRhaWxzKClgIHJl"
        "c29sdmVzCnRoZSBhY3RpbmcgdXNlcidzIG5hbWUsIGRlc2lnbmF0aW9uIGFuZCBwaG9uZSBmcm9tIHRoZWlyIHByb2ZpbGUsIGZhbGxpbmcgYm"
        "Fjawp0byBjb21wYW55IGRlZmF1bHRzLiBDb21iaW5lZCB3aXRoIHRoZSBSZXBseS1UbyB0aGUgb3V0Ym94IGFscmVhZHkgc2V0cywgYQpjYW5k"
        "aWRhdGUgY2FuIHJlcGx5IHRvIHRoZSByZWNydWl0ZXIgaGFuZGxpbmcgdGhlbSByYXRoZXIgdGhhbiBhIHNoYXJlZCBpbmJveC4KIiIiCmZyb2"
        "0gX19mdXR1cmVfXyBpbXBvcnQgYW5ub3RhdGlvbnMKCmltcG9ydCBvcwpmcm9tIGRhdGV0aW1lIGltcG9ydCBkYXRldGltZQpmcm9tIGh0bWwg"
        "aW1wb3J0IGVzY2FwZQpmcm9tIHR5cGluZyBpbXBvcnQgQW55CgojIC0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS"
        "0tLS0tLS0tLS0tLS0tLS0tLS0tLSBjb21wYW55IGJsb2NrCgoKZGVmIF9lbnYobmFtZTogc3RyLCBkZWZhdWx0OiBzdHIpIC0+IHN0cjoKICAg"
        "IHJldHVybiAob3MuZ2V0ZW52KG5hbWUpIG9yICIiKS5zdHJpcCgpIG9yIGRlZmF1bHQKCgpkZWYgY29tcGFueV9kZXRhaWxzKCkgLT4gZGljdF"
        "tzdHIsIHN0cl06CiAgICAiIiJTaWduYXR1cmUgZm9vdGVyLiBFbnYtb3ZlcnJpZGFibGUgc28gYSByZWJyYW5kIGlzIGEgY29uZmlnIGNoYW5n"
        "ZS4KCiAgICBEZWZhdWx0cyBtaXJyb3Igc2VydmljZXMvY29tcGFueV9pbnZvaWNlX2NvbmZpZy5weSBzbyB0aGUgaW52aXRhdGlvbiBhbmQgdG"
        "hlCiAgICB0YXggaW52b2ljZSBkbyBub3QgZGlzYWdyZWUgYWJvdXQgd2hvIEthcm5leCBpcy4KICAgICIiIgogICAgcmV0dXJuIHsKICAgICAg"
        "ICAibmFtZSI6IF9lbnYoIkNPTVBBTllfTkFNRSIsICJLYXJuZXggU29mdHdhcmUgU29sdXRpb25zIFBWVCBMVEQiKSwKICAgICAgICAic2hvcn"
        "RfbmFtZSI6IF9lbnYoIkNPTVBBTllfU0hPUlRfTkFNRSIsICJLYXJuZXgiKSwKICAgICAgICAicGhvbmUiOiBfZW52KCJDT01QQU5ZX1BIT05F"
        "IiwgX2VudigiSU5WT0lDRV9TRUxMRVJfUEhPTkUiLCAiIikpLAogICAgICAgICJlbWFpbCI6IF9lbnYoIkNPTVBBTllfRU1BSUwiLCBfZW52KC"
        "JJTlZPSUNFX1NFTExFUl9DT05UQUNUX0VNQUlMIiwgImluZm9Aa2FybmV4LmluIikpLAogICAgICAgICJ3ZWJzaXRlIjogX2VudigiQ09NUEFO"
        "WV9XRUJTSVRFIiwgImh0dHBzOi8va2FybmV4LmluLyIpLAogICAgfQoKCmRlZiBzZW5kZXJfZGV0YWlscyhkYiwgdXNlcikgLT4gZGljdFtzdH"
        "IsIHN0cl06CiAgICAiIiJXaG8gc2lnbnMgdGhlIGVtYWlsIOKAlCB0aGUgbG9nZ2VkLWluIHJlY3J1aXRlciwgbm90IGEgZ2VuZXJpYyBtYWls"
        "Ym94LgoKICAgIGB1c2VyYCBpcyB0aGUgQ3VycmVudFVzZXIuIE5hbWUgYW5kIGVtYWlsIGNvbWUgZnJvbSB0aGUgbG9naW4gcmVjb3JkOyB0aG"
        "UKICAgIGRlc2lnbmF0aW9uLCBkZXBhcnRtZW50IGFuZCBwaG9uZSBjb21lIGZyb20gdGhlaXIgVXNlclByb2ZpbGUsIHdoaWNoIGlzCiAgICBv"
        "cHRpb25hbCDigJQgc28gZXZlcnkgZmllbGQgZGVncmFkZXMgdG8gYSBjb21wYW55IGRlZmF1bHQgcmF0aGVyIHRoYW4gYSBibGFuay4KICAgIC"
        "IiIgogICAgY29tcGFueSA9IGNvbXBhbnlfZGV0YWlscygpCiAgICBuYW1lID0gKGdldGF0dHIodXNlciwgImZ1bGxfbmFtZSIsICIiKSBvciBn"
        "ZXRhdHRyKHVzZXIsICJ1c2VybmFtZSIsICIiKSBvciAiIikuc3RyaXAoKQogICAgZW1haWwgPSAoZ2V0YXR0cih1c2VyLCAiZW1haWwiLCAiIi"
        "kgb3IgIiIpLnN0cmlwKCkKICAgIGRlc2lnbmF0aW9uLCBkZXBhcnRtZW50LCBwaG9uZSA9ICIiLCAiIiwgIiIKCiAgICB0cnk6CiAgICAgICAg"
        "ZnJvbSBtb2RlbHMgaW1wb3J0IFVzZXJQcm9maWxlCgogICAgICAgIHByb2ZpbGUgPSBkYi5xdWVyeShVc2VyUHJvZmlsZSkuZmlsdGVyKFVzZX"
        "JQcm9maWxlLnVzZXJfaWQgPT0gZ2V0YXR0cih1c2VyLCAiaWQiLCBOb25lKSkuZmlyc3QoKQogICAgICAgIGlmIHByb2ZpbGUgaXMgbm90IE5v"
        "bmU6CiAgICAgICAgICAgIGRlc2lnbmF0aW9uID0gKHByb2ZpbGUuam9iX3RpdGxlIG9yICIiKS5zdHJpcCgpCiAgICAgICAgICAgIGRlcGFydG"
        "1lbnQgPSAocHJvZmlsZS5kZXBhcnRtZW50IG9yICIiKS5zdHJpcCgpCiAgICAgICAgICAgIHBob25lID0gKHByb2ZpbGUucGhvbmUgb3IgIiIp"
        "LnN0cmlwKCkKICAgIGV4Y2VwdCBFeGNlcHRpb246CiAgICAgICAgIyBBIG1pc3NpbmcgcHJvZmlsZSBtdXN0IG5ldmVyIHN0b3AgYW4gaW52aX"
        "RhdGlvbiBnb2luZyBvdXQuCiAgICAgICAgcGFzcwoKICAgIHJldHVybiB7CiAgICAgICAgIm5hbWUiOiBuYW1lIG9yIGNvbXBhbnlbInNob3J0"
        "X25hbWUiXSArICIgUmVjcnVpdG1lbnQgVGVhbSIsCiAgICAgICAgImRlc2lnbmF0aW9uIjogZGVzaWduYXRpb24sCiAgICAgICAgIyBUaGVpci"
        "B0ZW1wbGF0ZSBzaWducyBvZmYgIkh1bWFuIFJlc291cmNlcyI7IHVzZSB0aGUgc2VuZGVyJ3MgcmVhbAogICAgICAgICMgZGVwYXJ0bWVudCB3"
        "aGVuIHdlIGtub3cgaXQsIHNpbmNlIFRBIGFuZCBSTUcgYWxzbyBzZW5kIHRoZXNlLgogICAgICAgICJkZXBhcnRtZW50IjogZGVwYXJ0bWVudC"
        "BvciAiSHVtYW4gUmVzb3VyY2VzIiwKICAgICAgICAicGhvbmUiOiBwaG9uZSBvciBjb21wYW55WyJwaG9uZSJdLAogICAgICAgICJlbWFpbCI6"
        "IGVtYWlsIG9yIGNvbXBhbnlbImVtYWlsIl0sCiAgICB9CgoKIyAtLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS"
        "0tLS0tLS0tLS0tLS0tLS0tLS0tLS0gZm9ybWF0dGluZwoKCmRlZiBmb3JtYXRfd2hlbihyYXc6IHN0ciB8IE5vbmUpIC0+IHN0cjoKICAgICIi"
        "IiIyMDI2LTA4LTEwIDE2OjI0IiAtPiAiTW9uZGF5LCAxMCBBdWd1c3QgMjAyNiBhdCAxNjoyNCIuCgogICAgRmFsbHMgYmFjayB0byB3aGF0ZX"
        "ZlciB0aGUgcmVjcnVpdGVyIHR5cGVkIGlmIGl0IGRvZXMgbm90IHBhcnNlIOKAlCBhIHNsaWdodGx5CiAgICBvZGQgZGF0ZSBpbiB0aGUgZW1h"
        "aWwgYmVhdHMgYW4gZW1wdHkgb25lLgogICAgIiIiCiAgICB2YWx1ZSA9IChyYXcgb3IgIiIpLnN0cmlwKCkKICAgIGlmIG5vdCB2YWx1ZToKIC"
        "AgICAgICByZXR1cm4gIiIKICAgIGZvciBmbXQgaW4gKCIlWS0lbS0lZCAlSDolTSIsICIlWS0lbS0lZFQlSDolTSIsICIlWS0lbS0lZCAlSDol"
        "TTolUyIsICIlWS0lbS0lZFQlSDolTTolUyIpOgogICAgICAgIHRyeToKICAgICAgICAgICAgcmV0dXJuIGRhdGV0aW1lLnN0cnB0aW1lKHZhbH"
        "VlLCBmbXQpLnN0cmZ0aW1lKCIlQSwgJWQgJUIgJVkgYXQgJUg6JU0iKQogICAgICAgIGV4Y2VwdCBWYWx1ZUVycm9yOgogICAgICAgICAgICBj"
        "b250aW51ZQogICAgcmV0dXJuIHZhbHVlCgoKZGVmIF9yb3dzKHBhaXJzOiBsaXN0W3R1cGxlW3N0ciwgc3RyXV0pIC0+IGxpc3RbdHVwbGVbc3"
        "RyLCBzdHJdXToKICAgICIiIktlZXAgb25seSByb3dzIHdlIGNhbiBhY3R1YWxseSBmaWxsLiBTZWUgdGhlIG1vZHVsZSBkb2NzdHJpbmcuIiIi"
        "CiAgICByZXR1cm4gWyhsYWJlbCwgc3RyKHZhbHVlKS5zdHJpcCgpKSBmb3IgbGFiZWwsIHZhbHVlIGluIHBhaXJzCiAgICAgICAgICAgIGlmIH"
        "ZhbHVlIGlzIG5vdCBOb25lIGFuZCBzdHIodmFsdWUpLnN0cmlwKCldCgoKIyAtLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0t"
        "LS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLSB0ZW1wbGF0ZQoKCmRlZiBpbnRlcnZpZXdfaW52aXRlX21lc3NhZ2UoCiAgICAqLAogIC"
        "AgY2FuZGlkYXRlX25hbWU6IHN0ciwKICAgIHBvc2l0aW9uOiBzdHIsCiAgICBpbnRlcnZpZXdfbGV2ZWw6IHN0ciA9ICIiLAogICAgaW50ZXJ2"
        "aWV3X2RhdGU6IHN0ciA9ICIiLAogICAgZHVyYXRpb246IHN0ciA9ICIiLAogICAgaW50ZXJ2aWV3X21vZGU6IHN0ciA9ICIiLAogICAgbWVldG"
        "luZ19saW5rOiBzdHIgPSAiIiwKICAgIHZlbnVlOiBzdHIgPSAiIiwKICAgIGFjY2Vzc19rZXk6IHN0ciA9ICIiLAogICAgc2VuZGVyOiBkaWN0"
        "W3N0ciwgc3RyXSB8IE5vbmUgPSBOb25lLAogICAgY29tcGFueTogZGljdFtzdHIsIHN0cl0gfCBOb25lID0gTm9uZSwKKSAtPiBkaWN0W3N0ci"
        "wgc3RyXToKICAgICIiIkJ1aWxkIHRoZSBpbnZpdGF0aW9uLiBSZXR1cm5zIHsic3ViamVjdCIsICJ0ZXh0IiwgImh0bWwifS4iIiIKICAgIGNv"
        "bXBhbnkgPSBjb21wYW55IG9yIGNvbXBhbnlfZGV0YWlscygpCiAgICBzZW5kZXIgPSBzZW5kZXIgb3IgewogICAgICAgICJuYW1lIjogZiJ7Y2"
        "9tcGFueVsnc2hvcnRfbmFtZSddfSBSZWNydWl0bWVudCBUZWFtIiwKICAgICAgICAiZGVzaWduYXRpb24iOiAiIiwKICAgICAgICAiZGVwYXJ0"
        "bWVudCI6ICJIdW1hbiBSZXNvdXJjZXMiLAogICAgICAgICJwaG9uZSI6IGNvbXBhbnlbInBob25lIl0sCiAgICAgICAgImVtYWlsIjogY29tcG"
        "FueVsiZW1haWwiXSwKICAgIH0KCiAgICBzdWJqZWN0ID0gZiJJbnRlcnZpZXcgSW52aXRhdGlvbiDigJQge3Bvc2l0aW9ufSBhdCB7Y29tcGFu"
        "eVsnc2hvcnRfbmFtZSddfSIKCiAgICBkZXRhaWxfcm93cyA9IF9yb3dzKFsKICAgICAgICAoIkpvYiBQb3NpdGlvbiIsIHBvc2l0aW9uKSwKIC"
        "AgICAgICAoIkludGVydmlldyBMZXZlbCIsIGludGVydmlld19sZXZlbCksCiAgICAgICAgKCJEYXRlICYgVGltZSIsIGludGVydmlld19kYXRl"
        "KSwKICAgICAgICAoIkR1cmF0aW9uIiwgZHVyYXRpb24pLAogICAgICAgICgiSW50ZXJ2aWV3IE1vZGUiLCBpbnRlcnZpZXdfbW9kZSksCiAgIC"
        "AgICAgKCJMb2NhdGlvbiAvIE1lZXRpbmcgTGluayIsIG1lZXRpbmdfbGluayksCiAgICAgICAgKCJWZW51ZSBBZGRyZXNzIiwgdmVudWUpLAog"
        "ICAgXSkKCiAgICAjIC0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0gcGxhaW"
        "4gdGV4dAogICAgbGluZXMgPSBbCiAgICAgICAgZiJEZWFyIHtjYW5kaWRhdGVfbmFtZX0sIiwKICAgICAgICAiIiwKICAgICAgICBmIlRoYW5r"
        "IHlvdSBmb3IgeW91ciBpbnRlcmVzdCBpbiB0aGUge3Bvc2l0aW9ufSBwb3NpdGlvbiBhdCB7Y29tcGFueVsnbmFtZSddfS4gIgogICAgICAgIG"
        "YiRm9sbG93aW5nIGEgcmV2aWV3IG9mIHlvdXIgYXBwbGljYXRpb24sIHdlIGFyZSBwbGVhc2VkIHRvIGludml0ZSB5b3UgdG8gYXR0ZW5kICIK"
        "ICAgICAgICBmImFuIGludGVydmlldyBhcyBwYXJ0IG9mIG91ciBzZWxlY3Rpb24gcHJvY2Vzcy4iLAogICAgICAgICIiLAogICAgICAgICJJTl"
        "RFUlZJRVcgREVUQUlMUyIsCiAgICBdCiAgICB3aWR0aCA9IG1heCgobGVuKGxhYmVsKSBmb3IgbGFiZWwsIF8gaW4gZGV0YWlsX3Jvd3MpLCBk"
        "ZWZhdWx0PTApCiAgICBmb3IgbGFiZWwsIHZhbHVlIGluIGRldGFpbF9yb3dzOgogICAgICAgIGxpbmVzLmFwcGVuZChmIiAge2xhYmVsLmxqdX"
        "N0KHdpZHRoKX0gIHt2YWx1ZX0iKQogICAgaWYgYWNjZXNzX2tleToKICAgICAgICBsaW5lcyArPSBbIiIsIGYiICB7J1NlY3VyZSBBY2Nlc3Mg"
        "S2V5Jy5sanVzdCh3aWR0aCl9ICB7YWNjZXNzX2tleX0iLAogICAgICAgICAgICAgICAgICAiICBZb3Ugd2lsbCBuZWVkIHlvdXIgcmVnaXN0ZX"
        "JlZCBlbWFpbCBhbmQgdGhpcyBrZXkgdG8gZW50ZXIgdGhlIGludGVydmlldy4iLAogICAgICAgICAgICAgICAgICAiICBEbyBOT1Qgc2hhcmUg"
        "dGhlc2UgY3JlZGVudGlhbHMgd2l0aCBhbnlvbmUuIl0KICAgIGxpbmVzICs9IFsKICAgICAgICAiIiwKICAgICAgICAiUGxlYXNlIGNvbmZpcm"
        "0geW91ciBhdmFpbGFiaWxpdHkgYnkgcmVwbHlpbmcgdG8gdGhpcyBlbWFpbC4gSWYgdGhlIHByb3Bvc2VkICIKICAgICAgICAic2NoZWR1bGUg"
        "aXMgbm90IGNvbnZlbmllbnQsIGtpbmRseSBzaGFyZSB5b3VyIGF2YWlsYWJpbGl0eSBvbiB0aGlzIGVtYWlsLiIsCiAgICAgICAgIiIsCiAgIC"
        "AgICAgZiJXZSBhcHByZWNpYXRlIHlvdXIgaW50ZXJlc3QgaW4ge2NvbXBhbnlbJ3Nob3J0X25hbWUnXX0gYW5kIGxvb2sgZm9yd2FyZCB0byAi"
        "CiAgICAgICAgZiJzcGVha2luZyB3aXRoIHlvdS4iLAogICAgICAgICIiLAogICAgICAgICJQbGVhc2UgZW5zdXJlIHlvdSBqb2luIHRoZSBtZW"
        "V0aW5nIDUgbWludXRlcyBlYXJseSBhbmQgaGF2ZSBhIHN0YWJsZSBpbnRlcm5ldCAiCiAgICAgICAgImNvbm5lY3Rpb24gaW4gY2FzZSBvZiBh"
        "biBvbmxpbmUgbWVldGluZy4iLAogICAgICAgICIiLAogICAgICAgICJLaW5kIHJlZ2FyZHMsIiwKICAgICAgICBzZW5kZXJbIm5hbWUiXSwKIC"
        "AgIF0KICAgIGZvciBleHRyYSBpbiAoc2VuZGVyLmdldCgiZGVzaWduYXRpb24iKSwgc2VuZGVyLmdldCgiZGVwYXJ0bWVudCIpLCBjb21wYW55"
        "WyJuYW1lIl0pOgogICAgICAgIGlmIChleHRyYSBvciAiIikuc3RyaXAoKToKICAgICAgICAgICAgbGluZXMuYXBwZW5kKGV4dHJhKQogICAgaW"
        "Ygc2VuZGVyLmdldCgicGhvbmUiKToKICAgICAgICBsaW5lcy5hcHBlbmQoZiJQaG9uZToge3NlbmRlclsncGhvbmUnXX0iKQogICAgaWYgc2Vu"
        "ZGVyLmdldCgiZW1haWwiKToKICAgICAgICBsaW5lcy5hcHBlbmQoZiJFbWFpbDoge3NlbmRlclsnZW1haWwnXX0iKQogICAgaWYgY29tcGFueS"
        "5nZXQoIndlYnNpdGUiKToKICAgICAgICBsaW5lcy5hcHBlbmQoZiJXZWI6ICAge2NvbXBhbnlbJ3dlYnNpdGUnXX0iKQogICAgdGV4dCA9ICJc"
        "biIuam9pbihsaW5lcykgKyAiXG4iCgogICAgIyAtLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS"
        "0tLS0tLS0tLS0tLS0tLS0gaHRtbAogICAgcm93c19odG1sID0gIiIuam9pbigKICAgICAgICAnPHRyPicKICAgICAgICBmJzx0ZCBzdHlsZT0i"
        "cGFkZGluZzo5cHggMThweCA5cHggMDtjb2xvcjojNjQ3NDhiO2ZvbnQtc2l6ZToxM3B4OycKICAgICAgICBmJ3doaXRlLXNwYWNlOm5vd3JhcD"
        "t2ZXJ0aWNhbC1hbGlnbjp0b3A7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgI2YxZjVmOTsiPntlc2NhcGUobGFiZWwpfTwvdGQ+JwogICAgICAg"
        "IGYnPHRkIHN0eWxlPSJwYWRkaW5nOjlweCAwO2NvbG9yOiMwZjE3MmE7Zm9udC1zaXplOjE0cHg7Zm9udC13ZWlnaHQ6NjAwOycKICAgICAgIC"
        "BmJ2JvcmRlci1ib3R0b206MXB4IHNvbGlkICNmMWY1Zjk7d29yZC1icmVhazpicmVhay13b3JkOyI+e192YWx1ZV9odG1sKGxhYmVsLCB2YWx1"
        "ZSl9PC90ZD4nCiAgICAgICAgJzwvdHI+JwogICAgICAgIGZvciBsYWJlbCwgdmFsdWUgaW4gZGV0YWlsX3Jvd3MKICAgICkKCiAgICBrZXlfaH"
        "RtbCA9ICIiCiAgICBpZiBhY2Nlc3Nfa2V5OgogICAgICAgIGtleV9odG1sID0gZiIiIgogICAgICA8ZGl2IHN0eWxlPSJtYXJnaW46MThweCAw"
        "O3BhZGRpbmc6MTRweCAxOHB4O2JhY2tncm91bmQ6I2Y4ZmFmYztib3JkZXI6MnB4IGRhc2hlZCAjNGY0NmU1O2JvcmRlci1yYWRpdXM6MTJweD"
        "siPgogICAgICAgIDxwIHN0eWxlPSJtYXJnaW46MCAwIDRweDtmb250LXNpemU6MTFweDt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7bGV0dGVy"
        "LXNwYWNpbmc6MC4wOGVtO2NvbG9yOiM2NDc0OGI7Zm9udC13ZWlnaHQ6NzAwOyI+U2VjdXJlIEFjY2VzcyBLZXk8L3A+CiAgICAgICAgPHAgc3"
        "R5bGU9Im1hcmdpbjowO2ZvbnQtc2l6ZToyMHB4O2ZvbnQtd2VpZ2h0OjgwMDtsZXR0ZXItc3BhY2luZzowLjE1ZW07Y29sb3I6IzBmMTcyYTtm"
        "b250LWZhbWlseTpDb25zb2xhcyxtb25vc3BhY2U7Ij57ZXNjYXBlKGFjY2Vzc19rZXkpfTwvcD4KICAgICAgICA8cCBzdHlsZT0ibWFyZ2luOj"
        "hweCAwIDA7Zm9udC1zaXplOjEycHg7Y29sb3I6IzY0NzQ4YjsiPllvdSB3aWxsIG5lZWQgeW91ciByZWdpc3RlcmVkIGVtYWlsIGFuZCB0aGlz"
        "IGtleSB0byBlbnRlci4gUGxlYXNlIGRvIG5vdCBzaGFyZSB0aGVtLjwvcD4KICAgICAgPC9kaXY+IiIiCgogICAgc2lnX2xpbmVzID0gIiIuam"
        "9pbigKICAgICAgICBmJzxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxM3B4O2NvbG9yOiM0NzU1Njk7Ij57ZXNjYXBlKHYpfTwvZGl2PicKICAgICAg"
        "ICBmb3IgdiBpbiAoc2VuZGVyLmdldCgiZGVzaWduYXRpb24iKSwgc2VuZGVyLmdldCgiZGVwYXJ0bWVudCIpLCBjb21wYW55WyJuYW1lIl0pCi"
        "AgICAgICAgaWYgKHYgb3IgIiIpLnN0cmlwKCkKICAgICkKICAgIGNvbnRhY3RfbGluZXMgPSAiIgogICAgaWYgc2VuZGVyLmdldCgicGhvbmUi"
        "KToKICAgICAgICBjb250YWN0X2xpbmVzICs9IGYnPGRpdiBzdHlsZT0iZm9udC1zaXplOjEzcHg7Y29sb3I6IzQ3NTU2OTsiPiYjMTI4MjIyOy"
        "B7ZXNjYXBlKHNlbmRlclsicGhvbmUiXSl9PC9kaXY+JwogICAgaWYgc2VuZGVyLmdldCgiZW1haWwiKToKICAgICAgICBjb250YWN0X2xpbmVz"
        "ICs9IChmJzxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxM3B4O2NvbG9yOiM0NzU1Njk7Ij4mIzEyODIzMTsgJwogICAgICAgICAgICAgICAgICAgIC"
        "AgICAgIGYnPGEgaHJlZj0ibWFpbHRvOntlc2NhcGUoc2VuZGVyWyJlbWFpbCJdKX0iIHN0eWxlPSJjb2xvcjojNGY0NmU1O3RleHQtZGVjb3Jh"
        "dGlvbjpub25lOyI+JwogICAgICAgICAgICAgICAgICAgICAgICAgIGYne2VzY2FwZShzZW5kZXJbImVtYWlsIl0pfTwvYT48L2Rpdj4nKQogIC"
        "AgaWYgY29tcGFueS5nZXQoIndlYnNpdGUiKToKICAgICAgICBjb250YWN0X2xpbmVzICs9IChmJzxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxM3B4"
        "O2NvbG9yOiM0NzU1Njk7Ij4mIzEyNzc2MDsgJwogICAgICAgICAgICAgICAgICAgICAgICAgIGYnPGEgaHJlZj0ie2VzY2FwZShjb21wYW55Wy"
        "J3ZWJzaXRlIl0pfSIgc3R5bGU9ImNvbG9yOiM0ZjQ2ZTU7dGV4dC1kZWNvcmF0aW9uOm5vbmU7Ij4nCiAgICAgICAgICAgICAgICAgICAgICAg"
        "ICAgZid7ZXNjYXBlKGNvbXBhbnlbIndlYnNpdGUiXSl9PC9hPjwvZGl2PicpCgogICAgaHRtbCA9IGYiIiIKPGRpdiBzdHlsZT0iZm9udC1mYW"
        "1pbHk6J1NlZ29lIFVJJyxBcmlhbCxzYW5zLXNlcmlmO2xpbmUtaGVpZ2h0OjEuNjtjb2xvcjojMWUyOTNiO21heC13aWR0aDo2MjBweDsiPgog"
        "IDxkaXYgc3R5bGU9InBhZGRpbmc6MCAwIDE0cHg7Ym9yZGVyLWJvdHRvbToycHggc29saWQgI2UyZThmMDttYXJnaW4tYm90dG9tOjIwcHg7Ij"
        "4KICAgIDxzcGFuIHN0eWxlPSJmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjIwcHg7bGV0dGVyLXNwYWNpbmc6LTAuMDJlbTtjb2xvcjojMGYx"
        "NzJhOyI+S0FSTkVYPC9zcGFuPgogICAgPHNwYW4gc3R5bGU9ImZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MjBweDtsZXR0ZXItc3BhY2luZz"
        "otMC4wMmVtO2NvbG9yOiM0ZjQ2ZTU7Ij4gQ2FyZWVyczwvc3Bhbj4KICA8L2Rpdj4KCiAgPHAgc3R5bGU9Im1hcmdpbjowIDAgMTRweDtmb250"
        "LXNpemU6MTVweDsiPkRlYXIgPHN0cm9uZz57ZXNjYXBlKGNhbmRpZGF0ZV9uYW1lKX08L3N0cm9uZz4sPC9wPgoKICA8cCBzdHlsZT0ibWFyZ2"
        "luOjAgMCAxOHB4O2ZvbnQtc2l6ZToxNHB4O2NvbG9yOiMzMzQxNTU7Ij4KICAgIFRoYW5rIHlvdSBmb3IgeW91ciBpbnRlcmVzdCBpbiB0aGUg"
        "PHN0cm9uZz57ZXNjYXBlKHBvc2l0aW9uKX08L3N0cm9uZz4gcG9zaXRpb24gYXQKICAgIHtlc2NhcGUoY29tcGFueVsnbmFtZSddKX0uIEZvbG"
        "xvd2luZyBhIHJldmlldyBvZiB5b3VyIGFwcGxpY2F0aW9uLCB3ZSBhcmUgcGxlYXNlZCB0bwogICAgaW52aXRlIHlvdSB0byBhdHRlbmQgYW4g"
        "aW50ZXJ2aWV3IGFzIHBhcnQgb2Ygb3VyIHNlbGVjdGlvbiBwcm9jZXNzLgogIDwvcD4KCiAgPHAgc3R5bGU9Im1hcmdpbjowIDAgNnB4O2Zvbn"
        "Qtc2l6ZToxMnB4O3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtsZXR0ZXItc3BhY2luZzowLjA4ZW07Y29sb3I6IzRmNDZlNTtmb250LXdlaWdo"
        "dDo3MDA7Ij5JbnRlcnZpZXcgRGV0YWlsczwvcD4KICA8dGFibGUgcm9sZT0icHJlc2VudGF0aW9uIiBjZWxscGFkZGluZz0iMCIgY2VsbHNwYW"
        "Npbmc9IjAiIHN0eWxlPSJ3aWR0aDoxMDAlO2JvcmRlci1jb2xsYXBzZTpjb2xsYXBzZTttYXJnaW46MCAwIDRweDsiPgogICAge3Jvd3NfaHRt"
        "bH0KICA8L3RhYmxlPgogIHtrZXlfaHRtbH0KCiAgPHAgc3R5bGU9Im1hcmdpbjoxOHB4IDAgMDtmb250LXNpemU6MTRweDtjb2xvcjojMzM0MT"
        "U1OyI+CiAgICBQbGVhc2UgY29uZmlybSB5b3VyIGF2YWlsYWJpbGl0eSBieSByZXBseWluZyB0byB0aGlzIGVtYWlsLiBJZiB0aGUgcHJvcG9z"
        "ZWQgc2NoZWR1bGUgaXMKICAgIG5vdCBjb252ZW5pZW50LCBraW5kbHkgc2hhcmUgeW91ciBhdmFpbGFiaWxpdHkgb24gdGhpcyBlbWFpbC4KIC"
        "A8L3A+CiAgPHAgc3R5bGU9Im1hcmdpbjoxMnB4IDAgMDtmb250LXNpemU6MTRweDtjb2xvcjojMzM0MTU1OyI+CiAgICBXZSBhcHByZWNpYXRl"
        "IHlvdXIgaW50ZXJlc3QgaW4ge2VzY2FwZShjb21wYW55WydzaG9ydF9uYW1lJ10pfSBhbmQgbG9vayBmb3J3YXJkIHRvIHNwZWFraW5nIHdpdG"
        "ggeW91LgogIDwvcD4KICA8cCBzdHlsZT0ibWFyZ2luOjEycHggMCAwO2ZvbnQtc2l6ZToxM3B4O2NvbG9yOiM2NDc0OGI7Ij4KICAgIFBsZWFz"
        "ZSBlbnN1cmUgeW91IGpvaW4gdGhlIG1lZXRpbmcgNSBtaW51dGVzIGVhcmx5IGFuZCBoYXZlIGEgc3RhYmxlIGludGVybmV0IGNvbm5lY3Rpb2"
        "4KICAgIGluIGNhc2Ugb2YgYW4gb25saW5lIG1lZXRpbmcuCiAgPC9wPgoKICA8ZGl2IHN0eWxlPSJtYXJnaW4tdG9wOjI2cHg7cGFkZGluZy10"
        "b3A6MTZweDtib3JkZXItdG9wOjFweCBzb2xpZCAjZTJlOGYwOyI+CiAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6MTRweDtjb2xvcjojMzM0MT"
        "U1O21hcmdpbi1ib3R0b206NnB4OyI+S2luZCByZWdhcmRzLDwvZGl2PgogICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOjE1cHg7Zm9udC13ZWln"
        "aHQ6NzAwO2NvbG9yOiMwZjE3MmE7Ij57ZXNjYXBlKHNlbmRlclsnbmFtZSddKX08L2Rpdj4KICAgIHtzaWdfbGluZXN9CiAgICA8ZGl2IHN0eW"
        "xlPSJtYXJnaW4tdG9wOjhweDsiPntjb250YWN0X2xpbmVzfTwvZGl2PgogIDwvZGl2Pgo8L2Rpdj4KIiIiLnN0cmlwKCkKCiAgICByZXR1cm4g"
        "eyJzdWJqZWN0Ijogc3ViamVjdCwgInRleHQiOiB0ZXh0LCAiaHRtbCI6IGh0bWx9CgoKZGVmIF92YWx1ZV9odG1sKGxhYmVsOiBzdHIsIHZhbH"
        "VlOiBzdHIpIC0+IHN0cjoKICAgICIiIk1ha2UgdGhlIG1lZXRpbmcgbGluayBjbGlja2FibGU7IGV2ZXJ5dGhpbmcgZWxzZSBpcyBwbGFpbiBl"
        "c2NhcGVkIHRleHQuIiIiCiAgICBpZiBsYWJlbC5sb3dlcigpLnN0YXJ0c3dpdGgoImxvY2F0aW9uIikgYW5kIHZhbHVlLmxvd2VyKCkuc3Rhcn"
        "Rzd2l0aCgiaHR0cCIpOgogICAgICAgIHJldHVybiAoZic8YSBocmVmPSJ7ZXNjYXBlKHZhbHVlKX0iIHN0eWxlPSJjb2xvcjojNGY0NmU1O3Rl"
        "eHQtZGVjb3JhdGlvbjpub25lOycKICAgICAgICAgICAgICAgIGYnd29yZC1icmVhazpicmVhay1hbGw7Ij57ZXNjYXBlKHZhbHVlKX08L2E+Jy"
        "kKICAgIHJldHVybiBlc2NhcGUodmFsdWUpCgoKIyAtLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0t"
        "LS0tLS0tLS0tLS0tLS0tIGFzc2VtYmxpbmcKCgpkZWYgYnVpbGRfYWlfaW50ZXJ2aWV3X2ludml0ZSgKICAgIGRiLAogICAgdXNlciwKICAgIC"
        "osCiAgICBjYW5kaWRhdGVfbmFtZTogc3RyLAogICAgcG9zaXRpb246IHN0ciwKICAgIGxldmVsOiBzdHIgPSAiTDEiLAogICAgc2NoZWR1bGVk"
        "X2F0X3Jhdzogc3RyID0gIiIsCiAgICBpbnZpdGVfdXJsOiBzdHIgPSAiIiwKICAgIGFjY2Vzc19rZXk6IHN0ciA9ICIiLAopIC0+IGRpY3Rbc3"
        "RyLCBzdHJdOgogICAgIiIiVGhlIEFJIHNjcmVlbmluZyBpbnZpdGF0aW9uOiBhbHdheXMgb25saW5lLCBubyB2ZW51ZS4KCiAgICBEdXJhdGlv"
        "biBjb21lcyBmcm9tIElOVEVSVklFV19ERUZBVUxUX0RVUkFUSU9OIHdoZW4gc2V0LiBUaGUgQUkgYnJpZGdlIHJ1bnMKICAgIGNvdW50LW1vZG"
        "Ugd2l0aCBgdGltZV9saW1pdF9zZWM6IDBgIOKAlCB0aGVyZSBpcyBubyBmaXhlZCBsZW5ndGggdG8gcmVwb3J0IOKAlCBzbwogICAgcmF0aGVy"
        "IHRoYW4gaW52ZW50IG9uZSwgYW4gdW5zZXQgdmFsdWUgc2ltcGx5IGRyb3BzIHRoZSByb3cuCiAgICAiIiIKICAgIGxldmVsX2xhYmVsID0gey"
        "JMMSI6ICJMMSDigJQgQUkgU2NyZWVuaW5nIEludGVydmlldyIsCiAgICAgICAgICAgICAgICAgICAiTDIiOiAiTDIg4oCUIFRlY2huaWNhbCBJ"
        "bnRlcnZpZXcifS5nZXQoKGxldmVsIG9yICIiKS51cHBlcigpLCBsZXZlbCBvciAiIikKICAgIHJldHVybiBpbnRlcnZpZXdfaW52aXRlX21lc3"
        "NhZ2UoCiAgICAgICAgY2FuZGlkYXRlX25hbWU9Y2FuZGlkYXRlX25hbWUsCiAgICAgICAgcG9zaXRpb249cG9zaXRpb24sCiAgICAgICAgaW50"
        "ZXJ2aWV3X2xldmVsPWxldmVsX2xhYmVsLAogICAgICAgIGludGVydmlld19kYXRlPWZvcm1hdF93aGVuKHNjaGVkdWxlZF9hdF9yYXcpLAogIC"
        "AgICAgIGR1cmF0aW9uPV9lbnYoIklOVEVSVklFV19ERUZBVUxUX0RVUkFUSU9OIiwgIiIpLAogICAgICAgIGludGVydmlld19tb2RlPSJPbmxp"
        "bmUg4oCUIEFJIEludGVydmlldyAoYnJvd3NlciBiYXNlZCkiLAogICAgICAgIG1lZXRpbmdfbGluaz1pbnZpdGVfdXJsLAogICAgICAgIHZlbn"
        "VlPSIiLAogICAgICAgIGFjY2Vzc19rZXk9YWNjZXNzX2tleSwKICAgICAgICBzZW5kZXI9c2VuZGVyX2RldGFpbHMoZGIsIHVzZXIpLAogICAg"
        "KQo=",
}


def repo_root() -> str:
    here = os.path.abspath(os.getcwd())
    if os.path.isfile(os.path.join(here, "backend", "main.py")):
        return here
    parent = os.path.dirname(here)
    if os.path.isfile(os.path.join(parent, "backend", "main.py")):
        return parent
    print("ERROR: run this from F:\\AI-Interview-Model-B-V2 (or its backend folder).")
    raise SystemExit(2)


def _read(path: str) -> str:
    with io.open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _write(path: str, text: str) -> None:
    if CHECK_ONLY:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def write_files(root: str) -> None:
    print("=== 1. New template module ===")
    for rel, b64 in FILES.items():
        dest = os.path.join(root, rel.replace("/", os.sep))
        data = base64.b64decode(b64)
        old = io.open(dest, "rb").read() if os.path.exists(dest) else None
        if old == data:
            print("  unchanged  " + rel)
            continue
        if not CHECK_ONLY:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            io.open(dest, "wb").write(data)
        verb = "would update" if CHECK_ONLY else ("updated" if old else "created")
        print("  " + verb + "  " + rel)


def patch(root: str, rel: str, old: str, new: str, marker: str) -> bool:
    path = os.path.join(root, rel.replace("/", os.sep))
    if not os.path.exists(path):
        print(BAD + " missing " + rel)
        return False
    text = _read(path)
    if marker in text:
        print("  already applied  " + rel)
        return True
    found = text.count(old)
    if found != 1:
        print(BAD + " " + rel + ": anchor not found (" + str(found) +
              " matches) — the file has changed since this script was built")
        return False
    _write(path, text.replace(old, new))
    print("  " + ("would patch" if CHECK_ONLY else "patched") + "      " + rel)
    return True


OLD_SEND_INVITE = '''def _send_invite(db: Session, profile: CandidateProfile, candidate: Candidate,
                 requirement: Requirement | None, *, to_email: str, to_name: str,
                 invite_url: str, access_key: str, when_text: str) -> dict:
    msg = interview_link_message(to_name, _role_title(db, profile, requirement),
                                 when_text, invite_url, access_key)
    return notify_candidate(to_email, candidate.phone, msg["subject"], msg["text"], msg["html"])'''

NEW_SEND_INVITE = '''def _send_invite(db: Session, profile: CandidateProfile, candidate: Candidate,
                 requirement: Requirement | None, *, to_email: str, to_name: str,
                 invite_url: str, access_key: str, when_text: str,
                 user=None, level: str = "L1", scheduled_at_raw: str = "") -> dict:
    """Full invitation when we know who is sending it; the old short note otherwise.

    `user` is the acting CurrentUser — its name, designation and phone become the
    signature, so the candidate can see and reply to the person handling them.
    """
    position = _role_title(db, profile, requirement)
    if user is not None:
        from services.interview_invite_email import build_ai_interview_invite

        msg = build_ai_interview_invite(
            db, user,
            candidate_name=to_name,
            position=position,
            level=level or "L1",
            scheduled_at_raw=scheduled_at_raw,
            invite_url=invite_url,
            access_key=access_key,
        )
    else:
        msg = interview_link_message(to_name, position, when_text, invite_url, access_key)
    return notify_candidate(to_email, candidate.phone, msg["subject"], msg["text"], msg["html"])'''

OLD_CALL = '''            invite_url=bridge.get("invite_url", ""), access_key=bridge.get("access_key", ""),
            when_text=_when_text(when),
        )'''

NEW_CALL = '''            invite_url=bridge.get("invite_url", ""), access_key=bridge.get("access_key", ""),
            when_text=_when_text(when), user=user, level="L1", scheduled_at_raw=when,
        )'''


def wire_up(root: str) -> bool:
    print("")
    print("=== 2. Wiring the AI interview invite ===")
    a = patch(root, "backend/routers/crm/ai_interviews.py",
              OLD_SEND_INVITE, NEW_SEND_INVITE, "build_ai_interview_invite")
    b = patch(root, "backend/routers/crm/ai_interviews.py",
              OLD_CALL, NEW_CALL, "when_text=_when_text(when), user=user")
    return bool(a and b)


def configure_env(root: str) -> None:
    print("")
    print("=== 3. Company details (.env) ===")
    path = os.path.join(root, ".env")
    if not os.path.exists(path):
        print(WARN + " no .env found — skipping")
        return
    lines = _read(path).splitlines()
    have = set()
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            have.add(s.split("=", 1)[0].strip())

    missing = [(k, v) for k, v in ENV_DEFAULTS if k not in have]
    if not missing:
        print(OK + " all company keys already present")
        return
    if lines and lines[-1].strip():
        lines.append("")
    lines.append("# --- Interview invitation email (added by karnex_invite_template.py) ---")
    for key, value in missing:
        lines.append(key + "=" + value)
        shown = value if value else "(blank — that row is omitted from the email)"
        print("  added  " + key + "=" + shown)
    _write(path, "\n".join(lines) + "\n")


def _load_env(root: str) -> None:
    path = os.path.join(root, ".env")
    if not os.path.exists(path):
        return
    for line in _read(path).splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def preview(root: str) -> int:
    backend = os.path.join(root, "backend")
    sys.path.insert(0, backend)
    _load_env(root)
    os.chdir(backend)

    from services.interview_invite_email import interview_invite_message

    msg = interview_invite_message(
        candidate_name="Amulya H K",
        position="AUTOSAR Engineer",
        interview_level="L1 — AI Screening Interview",
        interview_date="Monday, 10 August 2026 at 16:51",
        duration=os.getenv("INTERVIEW_DEFAULT_DURATION", ""),
        interview_mode="Online — AI Interview (browser based)",
        meeting_link="https://karnex.in/?invite=SAMPLE",
        venue="",
        access_key="LC0DQI8G",
        sender={"name": "Pavan Sanap", "designation": "Talent Acquisition Lead",
                "department": "Human Resources", "phone": "+91 98765 43210",
                "email": "pavan.sanap@karnex.in"},
    )
    print("")
    print("SUBJECT: " + msg["subject"])
    print("=" * 72)
    print(msg["text"])
    print("=" * 72)
    out = os.path.join(root, "invite_preview.html")
    io.open(out, "w", encoding="utf-8").write(msg["html"])
    print("HTML version written to " + out)
    print("Open it in a browser to see exactly what the candidate receives.")
    return 0


def main() -> int:
    global CHECK_ONLY
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="dry run, change nothing")
    ap.add_argument("--preview", action="store_true", help="print a sample email")
    args = ap.parse_args()
    CHECK_ONLY = args.check

    root = repo_root()
    print("Karnex — interview invitation email")
    print("")
    print("Repo: " + root)
    print("")

    if args.preview:
        return preview(root)

    write_files(root)
    ok = wire_up(root)
    configure_env(root)

    print("")
    print("=" * 62)
    if not ok:
        print("NOT APPLIED — an anchor did not match. Paste this output back to Claude.")
        return 1
    if CHECK_ONLY:
        print("Dry run only — nothing written. Re-run without --check to apply.")
        return 0
    print("Applied. Next:")
    print("  1. python karnex_invite_template.py --preview   see the email")
    print("  2. restart the backend                          start_app.bat")
    print("  3. trigger an AI interview from the CRM         send a real one")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())