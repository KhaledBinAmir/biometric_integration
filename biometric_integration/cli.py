import click
from biometric_integration.commands import listener


@click.group()
def main():
    pass


main.add_command(listener)
