# MySQL deployment setup

The application uses SQLite by default for local development. To use MySQL,
create an empty MySQL database with your cloud provider, then set these
environment variables in the deployment service:

```text
DB_TYPE=mysql
DB_HOST=your-mysql-host
DB_PORT=3306
DB_USER=your-mysql-user
DB_PASSWORD=your-mysql-password
DB_NAME=your-mysql-database
```

Keep the real credentials in your provider's environment-variable settings or
in a local, untracked `.env` file. Never commit credentials to Git. Railway's
`MYSQLHOST`, `MYSQLPORT`, `MYSQLUSER`, `MYSQLPASSWORD`, and `MYSQLDATABASE`
variables are also recognized.

## Create the schema and copy the current SQLite data

Run these commands from the `Agri Supply` directory in an environment that can
connect to the cloud MySQL host. The MySQL database should be empty:

```powershell
python -m flask --app app init-db
python -m flask --app app migrate-sqlite-to-mysql
```

The migration command copies the local `database.db` data into MySQL. It is a
one-time copy, not a repeatable sync; do not run it again after a successful
migration because existing rows will cause it to stop and roll back.

After deploying, set the same MySQL environment variables in the web service.
For Render, set the service's Root Directory to `Agri Supply`, use
`pip install -r requirements.txt` as the build command, and
`gunicorn app:app` as the start command.
