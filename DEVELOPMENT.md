# Development

Install from source:

```sh
cd path/to/here
uv venv --python ">=3.14"
source .venv/bin/activate
uv pip install -e .
live --version
```

`live` is now on `$PATH` for the activated shell, and edits to `src/live/` take
effect on the next invocation.

## Tests

```sh
uv pip install pytest
pytest
```

Some completion tests are skipped unless `bash`, `zsh`, and/or `fish` are on `$PATH`.

## Release

1. Bump `version` in [`pyproject.toml`](pyproject.toml).
2. Commit and tag:

    ```sh
    git commit -am "release vX.Y.Z"
    git tag vX.Y.Z
    git push && git push --tags
    ```

3. Build sdist + wheel into `dist/`:

    ```sh
    rm -rf dist/
    uv build
    ```

4. Sanity-check the wheel installs cleanly in a throwaway env:

    ```sh
    uv tool install --from dist/astralarya_live-X.Y.Z-py3-none-any.whl astralarya-live
    live --version
    uv tool uninstall astralarya-live
    ```

5. Publish to TestPyPI first, install from it, smoke-test:

    ```sh
    uv publish --publish-url https://test.pypi.org/legacy/ --token "$TESTPYPI_TOKEN"
    uv tool install --index https://test.pypi.org/simple/ astralarya-live
    ```

6. Publish to PyPI:

    ```sh
    uv publish --token "$PYPI_TOKEN"
    ```

API tokens come from <https://pypi.org/manage/account/token/> (and the TestPyPI equivalent). Scope them to the `astralarya-live` project once it's been registered.
