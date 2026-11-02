# BMA alert relay (GitHub Actions)

ഈ repository ഓരോ ~5 മിനിറ്റിൽ NDMA SACHET Kerala ഫീഡ് നോക്കി, പുതിയ ഔദ്യോഗിക മുന്നറിയിപ്പ്
BMA LIFE CARE ആപ്പുകളിലേക്ക് (Firebase FCM topic വഴി) അയയ്ക്കുന്നു. ലാപ്ടോപ്പ് ഓൺ ആകേണ്ടതില്ല.

GitHub Secret-ന്റെ പേര്: FIREBASE_SERVICE_ACCOUNT
Value: Firebase console > Project settings > Service accounts > "Generate new private key" വഴി കിട്ടുന്ന
.json ഫയലിന്റെ ഉള്ളടക്കം മുഴുവൻ (ഫയലിന്റെ പേര് ...firebase-adminsdk-... എന്നിങ്ങനെയായിരിക്കും).

⚠ ആ key ഫയൽ ഒരിക്കലും ഈ repo-യിൽ upload ചെയ്യരുത്.
