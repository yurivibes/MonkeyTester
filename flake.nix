{
  description = "MonkeyTester: web viewer + test runner for AI-driven MonkeyTester tests";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachSystem [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ] (system:
      let
        pkgs = import nixpkgs { inherit system; };

        # The viewer and runner are stdlib-only. pillow is only for the optional
        # mt-tests/scripts/generate_placeholders.py helper.
        python = pkgs.python3.withPackages (ps: [ ps.pillow ]);

        # app.py converts recordings to WebM (libvpx-vp9 + libopus).
        runtimeInputs = [ python pkgs.ffmpeg ];

        # The viewer keeps its state next to app.py (Run/, Conf/, build_config.json),
        # so it runs from the checkout, never from the
        # read-only Nix store. Run these from the repo root.
        requireCheckout = ''
          if [ ! -f app.py ] || [ ! -d mt-tests/scripts ]; then
            echo "Run this from the MonkeyTester checkout (the directory with app.py)." >&2
            exit 1
          fi
        '';

        viewer = pkgs.writeShellApplication {
          name = "monkeytester-viewer";
          inherit runtimeInputs;
          text = ''
            ${requireCheckout}
            exec python3 app.py "$@"
          '';
        };

        # Same command as .github/workflows/test.yml: every tests/**/test_*.py.
        tests = pkgs.writeShellApplication {
          name = "monkeytester-tests";
          inherit runtimeInputs;
          text = ''
            ${requireCheckout}
            if [ "$#" -eq 0 ]; then
              set -- discover -s tests -t .
            fi
            exec python3 -m unittest "$@"
          '';
        };
      in {
        packages.default = viewer;
        packages.monkeytester-viewer = viewer;
        packages.tests = tests;

        # nix run              -> viewer on 0.0.0.0:8089 (nix run . -- <port> for another port)
        # nix run .#tests      -> the CI unit tests (or: nix run .#tests -- tests.test_app_api ...)
        apps.default = { type = "app"; program = "${viewer}/bin/monkeytester-viewer"; };
        apps.tests = { type = "app"; program = "${tests}/bin/monkeytester-tests"; };

        devShells.default = pkgs.mkShell { packages = runtimeInputs; };
      });
}
