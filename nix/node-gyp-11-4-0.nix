{
  buildNpmPackage,
  fetchFromGitHub,
  nodejs,
  lib,
}:

let
  node-gyp-11_4_0 = buildNpmPackage rec {
    pname = "node-gyp";
    version = "11.4.0";

    src = fetchFromGitHub {
      owner = "nodejs";
      repo = "node-gyp";
      rev = "refs/tags/v${version}";
      hash = "sha256-VtomUV+0kTp34IuS0D0dR4ZMWpk4Ptpk1CBP8rdW2a4=";
    };

    postPatch = ''
      # Mocha 11 still pins vulnerable diff 7 / serialize-javascript 6.
      # Keep its runner API; override only these dev/test dependencies.
      substituteInPlace package.json --replace-fail '"scripts": {' '"overrides": {"mocha": {"diff": "8.0.3", "serialize-javascript": "7.1.3"}}, "scripts": {'
      ln -s ${./node-gyp-11-4-0-package-lock.json} package-lock.json
    '';

    npmDepsHash = "sha256-VD4CURguuANBlzfsrHBXFDW/8bCV4P5RtQf+bYpZU4c=";

    npmDepsFetcherVersion = 2;

    dontNpmBuild = true;

    makeWrapperArgs = [ "--set npm_config_nodedir ${nodejs}" ];

    meta = {
      description = "Node.js native addon build tool";
      homepage = "https://github.com/nodejs/node-gyp";
      license = lib.licenses.mit;
      mainProgram = "node-gyp";
    };
  };
in
node-gyp-11_4_0
