import { Loader } from 'lucide-react';

export default function LoadingSpinner() {
  return (
    <div className="min-h-screen flex items-center justify-center">
      <Loader className="w-8 h-8 text-primary-500 animate-spin" />
    </div>
  );
}
