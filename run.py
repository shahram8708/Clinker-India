from app import create_app

app = create_app()

if __name__ == "__main__":
    # Disable the reloader; database tables are already created inside create_app.
    app.run(use_reloader=False)
