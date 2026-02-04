import { Loader } from 'lucide-react';

export default function LoadingSpinner() {
  return (
    <div className="min-h-screen bg-gray-50 dark:bg-gray-900 flex items-center justify-center">
      <Loader className="w-8 h-8 text-blue-500 animate-spin" />
    </div>
  );
}
