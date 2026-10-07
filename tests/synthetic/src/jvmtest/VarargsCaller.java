package jvmtest;

// Caller for tests/bn_varargs_check.py (jvm-64); each method is one case, the check names the expected text.
public class VarargsCaller {
    public static String spread() {
        return Varargs.join(",", "a", "b", "c");
    }

    public static String none() {
        return Varargs.join(";");
    }

    public static String explicitArray(String[] parts) {
        return Varargs.join("-", parts);
    }

    public static int instance(Varargs v) {
        return v.sum(1, 2, 3);
    }

    public static String overloaded(Varargs v) {
        return v.tag(new Object[]{"x"});
    }

    public static String overloadedSpread(Varargs v) {
        return v.tag(1, 2);
    }

    public static String nullArray() {
        return Varargs.join("+", (String[]) null);
    }

    public static int notVarargs() {
        return Varargs.count(new String[]{"p", "q"});
    }
}
