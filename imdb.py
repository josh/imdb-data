import click


@click.command()
def main() -> None:
    click.echo("Hello, World!")


if __name__ == "__main__":
    main()
