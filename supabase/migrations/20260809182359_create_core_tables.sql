-- 1. Create Users table (Links to Supabase's built-in Auth system)
CREATE TABLE public.users (
    id UUID REFERENCES auth.users(id) PRIMARY KEY,
    email TEXT NOT NULL,
    username TEXT,
    credits INTEGER DEFAULT 0 NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL
);

-- 2. Create Transactions table (Tracks user payments and credit reloads)
CREATE TABLE public.transactions (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id UUID REFERENCES public.users(id) NOT NULL,
    amount NUMERIC(10, 2) NOT NULL,
    credits_added INTEGER NOT NULL,
    status TEXT NOT NULL, -- e.g., 'completed', 'pending', 'failed'
    payment_provider_ref TEXT, -- e.g., Stripe session ID or eSewa transaction ID
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL
);

-- 3. Create Document Generations table (Tracks the ASYCUDA XML lifecycle)
CREATE TABLE public.document_generations (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id UUID REFERENCES public.users(id) NOT NULL,
    actor TEXT NOT NULL, -- Solves EC-14: Audit trail non-repudiation gap
    original_file_name TEXT NOT NULL,
    storage_key TEXT NOT NULL,
    mistral_file_id TEXT, -- Solves EC-07: Track vendor file ID for cleanup
    status TEXT NOT NULL, -- e.g., 'UPLOADED', 'PROCESSING', 'XML_READY', 'FAILED'
    xml_output TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL
);

-- Optional: Add Row Level Security (RLS) so users can only see their own data
ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.document_generations ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own profile" ON public.users FOR SELECT USING (auth.uid() = id);
CREATE POLICY "Users can view own transactions" ON public.transactions FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Users can view own documents" ON public.document_generations FOR SELECT USING (auth.uid() = user_id);