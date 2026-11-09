export function validateAccountExport(blob: Blob): Promise<{
  blob: Blob; filename: string; format: 'manifest' | 'zip';
}>;
