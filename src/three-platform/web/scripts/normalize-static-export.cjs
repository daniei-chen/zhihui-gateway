const fs = require('node:fs');
const path = require('node:path');

// Next 16's exporter uses OS path separators when collecting RSC segments,
// but its browser URL encoder only replaces '/'. Normalize generated files,
// not JavaScript, so Windows exports have the same names as Linux exports.
function normalizeStaticExport(directory) {
  const root = fs.realpathSync(directory);
  const files = [];
  function visit(dir) {
    for (const name of fs.readdirSync(dir)) {
      const file = path.join(dir, name);
      const stat = fs.lstatSync(file);
      if (stat.isSymbolicLink()) throw new Error('Static export must not contain links');
      if (stat.isDirectory()) visit(file);
      else if (stat.isFile()) files.push(file);
      else throw new Error('Static export must contain only regular files');
    }
  }
  visit(root);
  const changes = [];
  for (const file of files) {
    const parts = path.relative(root, file).split(path.sep);
    const index = parts.findIndex(part => part.startsWith('__next.'));
    if (index < 0 || index === parts.length - 1 || !file.endsWith('.txt')) continue;
    const destination = path.resolve(root, ...parts.slice(0, index), parts.slice(index).join('.'));
    if (!file.startsWith(root + path.sep) || !destination.startsWith(root + path.sep)) {
      throw new Error('Static export target escaped its output directory');
    }
    if (fs.existsSync(destination)) {
      if (!fs.readFileSync(file).equals(fs.readFileSync(destination))) throw new Error('RSC segment filename collision');
      fs.unlinkSync(file);
    } else fs.renameSync(file, destination);
    changes.push({from: path.relative(root, file), to: path.relative(root, destination)});
  }
  return changes;
}

module.exports = {normalizeStaticExport};
if (require.main === module) {
  const changes = normalizeStaticExport(path.resolve(__dirname, '../out'));
  console.log(`Normalized ${changes.length} Windows RSC segment filenames; file contents unchanged.`);
}
