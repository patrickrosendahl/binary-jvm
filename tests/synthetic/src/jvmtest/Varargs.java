package jvmtest;

// Callee for tests/bn_varargs_check.py (jvm-64): varargs methods declared in the same "jar" (directory) as
// their caller, so Pseudo Java reads ACC_VARARGS from this class file (jvm-57).
public class Varargs {
    private final String prefix;

    public Varargs(String prefix) {
        this.prefix = prefix;
    }

    public static String join(String sep, String... parts) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < parts.length; i++) {
            if (i > 0) {
                sb.append(sep);
            }
            sb.append(parts[i]);
        }
        return sb.toString();
    }

    public int sum(int... xs) {
        int n = 0;
        for (int x : xs) {
            n += x;
        }
        return n;
    }

    public String tag(Object... values) {
        return this.prefix + values.length;
    }

    // same name, fixed arity: tag(x) must not lose its array when it would pick this one
    public String tag(String value) {
        return this.prefix + value;
    }

    // an array parameter that is not varargs: its array literal stays
    public static int count(String[] items) {
        return items.length;
    }
}
