const inputPath = process.env.LAUNCHPLANE_OPENAPI_INPUT;
const outputPath = process.env.LAUNCHPLANE_OPENAPI_OUTPUT;

if (!inputPath || !outputPath) {
  throw new Error(
    "LAUNCHPLANE_OPENAPI_INPUT and LAUNCHPLANE_OPENAPI_OUTPUT are required for OpenAPI type generation.",
  );
}

export default {
  input: inputPath,
  output: outputPath,
  plugins: [
    {
      comments: false,
      name: "@hey-api/typescript",
    },
  ],
};
