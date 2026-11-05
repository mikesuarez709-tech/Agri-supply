# Email setup

Email verification and password recovery require an SMTP account. The application
does not send mail until the settings below are configured. Keep credentials in
environment variables; do not put real passwords in source files.

For Gmail, use `smtp.gmail.com`, port `587`, and TLS. Gmail SMTP authentication
requires a Google App Password (with 2-Step Verification enabled); do not use
your regular Google account password.

In PowerShell, set the values in the same terminal used to start Flask:

```powershell
$env:MAIL_SERVER = "smtp.gmail.com"
$env:MAIL_PORT = "587"
$env:MAIL_USERNAME = "your-account@gmail.com"
$env:MAIL_PASSWORD = "your-google-app-password"
$env:MAIL_DEFAULT_SENDER = "your-account@gmail.com"
$env:MAIL_USE_TLS = "true"
$env:MAIL_USE_SSL = "false"
$env:AGRI_SECRET_KEY = "<a-long-random-secret>"
$env:AGRI_PUBLIC_URL = "http://127.0.0.1:5000"
flask --app app run
```

Set `AGRI_PUBLIC_URL` to the externally reachable HTTPS origin when deploying;
verification links must point to the deployed application. Use a newly generated,
private `AGRI_SECRET_KEY` in every deployment. Existing accounts are treated as
verified during the database migration; newly registered accounts must verify
their email. Verification and password-reset links expire after one hour and can
only be used once.

After setting the SMTP variables, send a harmless test message to an address
you control:

```powershell
flask --app app test-email --email "you@example.com"
```

To run the local regression tests for the sales CSV export and email test command:

```powershell
python -m unittest discover -s tests -v
```

The admin order-status controls also email the customer when an order status
changes. Customers without a verified email are not sent notifications. An SMTP
failure does not undo the saved status; the admin sees a warning and the failure
is recorded in the application log.
